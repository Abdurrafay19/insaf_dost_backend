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
        print("\n[BOOT] Starting background AI initialization...")
        reasoner = ChatGroq(
            model="openai/gpt-oss-120b",
            temperature=0.0,
            api_key=settings.groq_api_key,
            max_retries=2,
        )
        fast_llm = ChatGroq(
            model="openai/gpt-oss-20b",
            temperature=0.0,
            api_key=settings.groq_api_key,
            max_retries=2,
        )
        vectorstore = get_async_vectorstore(
            settings.qdrant_url, settings.qdrant_api_key
        )

        try:
            from sentence_transformers import CrossEncoder

            reranker = CrossEncoder(
                "BAAI/bge-reranker-base", max_length=512, device="cpu"
            )
            print("[BOOT] Reranker model loaded successfully.")
        except Exception as e:
            print(f"[BOOT] Reranker failed to load: {e}")
            reranker = None

        gc.collect()

        app.state.insaf_graph = build_async_graph(
            reasoner, fast_llm, vectorstore, reranker
        )
        app.state.ready = True
        print("[BOOT] LangGraph compiled and server is ready for inference.\n")
    except Exception as e:
        print(f"[BOOT] CRITICAL: Initialization failed: {e}")
        app.state.ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.ready = False
    app.state.insaf_graph = None

    task = asyncio.create_task(background_loader(app, settings))
    yield
    task.cancel()
    app.state.insaf_graph = None
    app.state.ready = False
    gc.collect()


app = FastAPI(title="InsafDost AI API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://insafdostai.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_graph():
    if not getattr(app.state, "ready", False) or app.state.insaf_graph is None:
        raise HTTPException(status_code=503, detail="AI models are still initializing.")
    return app.state.insaf_graph


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "InsafDost AI Gateway"}


@app.get("/ready")
async def readiness_check():
    if getattr(app.state, "ready", False):
        return {"status": "ready"}
    raise HTTPException(status_code=503, detail="AI models are still loading.")


@app.post("/analyze", response_model=CaseResponse)
async def analyze_cases(request: CaseRequest, graph=Depends(get_graph)):
    print(f"\n=======================================================")
    print(f"[REQUEST] Received batch of {len(request.cases)} case(s)")
    print(f"=======================================================")

    if not request.cases:
        raise HTTPException(status_code=400, detail="No cases provided.")

    results = []

    # Sequential execution with backoff pacing to protect Groq TPM limits
    for i, case_text in enumerate(request.cases):
        print(f"\n[REQUEST] ---> Processing case [{i+1}/{len(request.cases)}]")
        max_retries = 3
        backoff = 10.0

        for attempt in range(max_retries):
            try:
                res = await graph.ainvoke({"raw_text": case_text})
                res_dict = dict(res)
                res_dict["_case_num"] = i + 1
                results.append(res_dict)
                print(
                    f"[REQUEST] ---> Case [{i+1}] finished. Final category: '{res_dict.get('category')}'"
                )
                break
            except Exception as e:
                err_str = str(e)
                print(
                    f"[REQUEST] Error during case [{i+1}] attempt {attempt + 1}: {err_str}"
                )
                if "429" in err_str or "rate_limit_exceeded" in err_str:
                    if attempt < max_retries - 1:
                        print(
                            f"[REQUEST] Rate limit reached. Sleeping {backoff}s before retry..."
                        )
                        await asyncio.sleep(backoff)
                        backoff *= 1.5
                        continue
                raise HTTPException(
                    status_code=500, detail=f"Inference error during case {i+1}."
                )

        # 2-second replenishment buffer between cases
        if i < len(request.cases) - 1:
            print("[REQUEST] Pausing 2.0s between cases to replenish tokens...")
            await asyncio.sleep(2.0)

    print(
        f"\n[REQUEST] Batch processing completed successfully for {len(results)} case(s).\n"
    )
    return {"status": "success", "data": results}
