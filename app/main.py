import gc
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Dict, Any

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from langchain_groq import ChatGroq
from app.core.config import get_settings
from app.services.vectorstore import get_async_vectorstore
from app.workflows.graph import build_async_graph

logging.basicConfig(level=logging.INFO)

class CaseRequest(BaseModel):
    cases: List[str]

class CaseResponse(BaseModel):
    status: str
    data: List[Dict[str, Any]]

async def background_loader(app: FastAPI, settings):
    try:
        logging.info("Starting background AI models download...")
        reasoner = ChatGroq(model="openai/gpt-oss-120b", temperature=0.0, api_key=settings.groq_api_key)
        fast_llm = ChatGroq(model="openai/gpt-oss-20b", temperature=0.0, api_key=settings.groq_api_key)
        vectorstore = get_async_vectorstore(settings.qdrant_url, settings.qdrant_api_key)
        
        # Load cross encoder in low memory footprint
        try:
            from sentence_transformers import CrossEncoder
            reranker = CrossEncoder("BAAI/bge-reranker-base", max_length=512, device="cpu")
            logging.info("Reranker loaded successfully.")
        except Exception as e: 
            logging.error(f"Reranker failed to load: {e}")
            reranker = None
            
        gc.collect()

        app.state.insaf_graph = build_async_graph(reasoner, fast_llm, vectorstore, reranker)
        app.state.ready = True
        logging.info("Graph compiled successfully and ready to serve requests.")
    except Exception as e:
        logging.error(f"Failed to initialize AI models: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.ready = False
    app.state.insaf_graph = None
    
    # Launch background loading task to prevent blocking container readiness check
    task = asyncio.create_task(background_loader(app, settings))
    
    yield
    
    task.cancel()
    app.state.insaf_graph = None
    app.state.ready = False
    gc.collect()

app = FastAPI(title="InsafDost AI API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://insafdostai.vercel.app"], 
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_graph():
    if not getattr(app.state, "ready", False) or app.state.insaf_graph is None:
        raise HTTPException(status_code=503, detail="AI Models are still loading.")
    return app.state.insaf_graph

@app.get("/health")
async def health_check():
    # Liveness check: Is the API server running?
    return {"status": "healthy", "service": "InsafDost AI Gateway"}

@app.get("/ready")
async def readiness_check():
    # Readiness check: Is the Graph compiled and ready for traffic?
    if getattr(app.state, "ready", False):
        return {"status": "ready"}
    raise HTTPException(status_code=503, detail="AI Models are still loading.")

@app.post("/analyze", response_model=CaseResponse)
async def analyze_cases(request: CaseRequest, graph=Depends(get_graph)):
    if not request.cases:
        raise HTTPException(status_code=400, detail="No cases provided.")

    results = []
    
    # Process cases sequentially with brief pacing to respect Groq's 8,000 TPM limit
    for i, case_text in enumerate(request.cases):
        max_retries = 3
        backoff = 10.0  # Groq's error requested ~9.03s
        
        for attempt in range(max_retries):
            try:
                res = await graph.ainvoke({"raw_text": case_text})
                res_dict = dict(res)
                res_dict['_case_num'] = i + 1
                results.append(res_dict)
                break
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "rate_limit_exceeded" in err_str:
                    if attempt < max_retries - 1:
                        logging.warning(f"Hit TPM rate limit on case {i+1}. Pausing for {backoff}s before retry...")
                        await asyncio.sleep(backoff)
                        backoff *= 1.5
                        continue
                logging.error(f"Inference execution error on case {i+1}: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Inference execution error during case {i+1}.")

        # Small 2-second buffer between cases to allow token window replenishment
        if i < len(request.cases) - 1:
            await asyncio.sleep(2.0)
            
    return {"status": "success", "data": results}