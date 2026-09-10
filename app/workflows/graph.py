import re
import asyncio
import numpy as np
from typing import TypedDict, List, Dict, Any, Literal
from pydantic import BaseModel
from langgraph.graph import StateGraph, START, END
from langchain_core.prompts import ChatPromptTemplate

class GuardrailOutput(BaseModel):
    is_valid: bool

class ProcessorOutput(BaseModel):
    category: Literal["Criminal", "Civil", "Family"]
    keywords: str

class InsafState(TypedDict):
    raw_text: str
    is_valid: bool
    category: str
    legal_keywords: str
    precedents: List[str]
    precedent_meta: List[dict]
    final_answer: str
    audit_score: float

class AsyncGraphNodes:
    def __init__(self, reasoner, fast_llm, vectorstore, reranker):
        self.reasoner = reasoner
        self.fast_llm = fast_llm
        self.vectorstore = vectorstore
        self.reranker = reranker
        try:
            self.guardrail_llm = fast_llm.with_structured_output(GuardrailOutput)
            self.processor_llm = fast_llm.with_structured_output(ProcessorOutput)
        except Exception as e:
            print(f"Structured output disabled: {e}")
            self.guardrail_llm = None
            self.processor_llm = None

    async def guardrail_node(self, state: InsafState):
        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are an automated legal classification gateway. Determine whether the input is a valid legal case, question, or scenario."),
            ("human", "{raw_text}")
        ])
        
        try:
            if self.guardrail_llm is None:
                raise RuntimeError("Structured output not available.")
            
            # Pipe prompt directly to retain chat message roles for tool-calling
            chain = prompt | self.guardrail_llm
            data = await chain.ainvoke({"raw_text": state["raw_text"]})
            is_valid = bool(data.is_valid)
        except Exception as e:
            # Logs the exact API error in the container console if Groq rejects the payload
            print(f"Guardrail failed to invoke LLM: {e}")
            is_valid = False

        if not is_valid:
            return {
                "is_valid": False,
                "category": "Irrelevant",
                "final_answer": "This does not appear to be a valid legal scenario. Please provide a relevant legal case.",
                "audit_score": 1.0,
                "precedents": [],
                "precedent_meta": []
            }

        return {"is_valid": True}

    async def processor_node(self, state: InsafState):
        prompt = ChatPromptTemplate.from_messages([
            ("system", "Analyze the following case facts. Extract the core legal doctrines, statutes, and formal legal terminology necessary to search a vector database of Pakistani case law. Output them as a single search string."),
            ("human", "{raw_text}")
        ])
        
        try:
            if self.processor_llm is None:
                raise RuntimeError("Structured output not available.")
            chain = prompt | self.processor_llm
            data = await chain.ainvoke({"raw_text": state["raw_text"]})
            category = data.category if data.category in {"Criminal", "Civil", "Family"} else "Civil"
            keywords = data.keywords or state["raw_text"][:50]
        except Exception as e:
            print(f"Processor failed to invoke LLM: {e}")
            category = "Civil"
            keywords = state["raw_text"][:50]
            
        return {"category": category, "legal_keywords": keywords}

    async def retriever_node(self, state: InsafState):
        query = state["legal_keywords"]
        raw_docs = await self.vectorstore.asimilarity_search_with_score(query, k=10)

        if self.reranker and raw_docs:
            full_query = f"Law of Pakistan regarding {state['category']}: {state['legal_keywords']}"
            pairs = [[full_query, doc.page_content[:1200]] for doc, _ in raw_docs]

            try:
                # Offload CPU-bound cross-encoder calculation to a background thread
                rr_scores = await asyncio.to_thread(self.reranker.predict, pairs)
                if isinstance(rr_scores, (list, tuple, np.ndarray)):
                    rr_scores_list = [float(x) for x in rr_scores]
                else:
                    rr_scores_list = [float(rr_scores)]

                # Sigmoid thresholding instead of arbitrary scalar cuts
                def sigmoid(x):
                    return 1 / (1 + np.exp(-x))

                valid_ranked = []
                for (doc, _), raw_score in zip(raw_docs, rr_scores_list):
                    prob = sigmoid(raw_score)
                    if prob > 0.5: 
                        valid_ranked.append((doc, prob))
                        
                ranked = sorted(valid_ranked, key=lambda x: x[1], reverse=True)[:3]

                if not ranked:
                    return {
                        "precedents": ["No strictly relevant Pakistani law precedents were found for this specific query."],
                        "precedent_meta": [{"source": "System", "score": 0.0}]
                    }

                final_docs = [doc for doc, _ in ranked]
                final_scores = [score for _, score in ranked]
            except Exception as e:
                print(f"Reranking failed, falling back: {e}")
                final_docs = [doc for (doc, _) in raw_docs[:3]]
                final_scores = [score for (_, score) in raw_docs[:3]]
        else:
            final_docs = [doc for (doc, _) in raw_docs[:3]]
            final_scores = [score for (_, score) in raw_docs[:3]]

        return {
            "precedents": [doc.page_content for doc in final_docs],
            "precedent_meta": [
                {"source": doc.metadata.get("source", "Unknown"), "score": round(float(score), 3)}
                for doc, score in zip(final_docs, final_scores)
            ]
        }

    async def reasoner_node(self, state: InsafState):
        precedents = state.get("precedents", [])
        precedent_meta = state.get("precedent_meta", [])
        context_lines = []
        for i, p in enumerate(precedents):
            meta_item = precedent_meta[i] if i < len(precedent_meta) else {}
            source = meta_item.get("source", "Unknown") if isinstance(meta_item, dict) else "Unknown"
            context_lines.append(f"[{i+1}] Authority: {source}\n{p}")
        context = "\n".join(context_lines)
        
        prompt = ChatPromptTemplate.from_template(
            "Act as a senior litigator in Pakistan. Using ONLY the following precedents: {context}.\n\n"
            "Analyze this Case: {raw_text}\n\n"
            "SYSTEM REQUIREMENT: You MUST format your entire response using Markdown syntax. "
            "You MUST explicitly begin each primary section with the literal markdown characters '### ' "
            "followed by the exact title name. Do not omit the '### ' symbols under any circumstances.\n\n"
            "### 1. Core Legal Issue\n\n"
            "### 2. Applicable Law & Precedents (Use [Number] citations)\n\n"
            "### 3. Case Analysis\n\n"
            "### 4. Actionable Litigation Strategy\n\n"
            "CRITICAL RULES: \n"
            "- Under 'Actionable Litigation Strategy', you MUST outline exact court filings, jurisdictional forums (e.g., Civil Court / High Court), and evidentiary requirements.\n"
            "- DO NOT provide generic administrative advice like 'update policies' or 'train staff'. Focus entirely on winning the case in court.\n"
            "- MUST leave a clear blank line (double newline) between headers, paragraphs, and list items so standard Markdown parsers can compile it perfectly."
        )
        formatted_prompt = prompt.format(context=context, raw_text=state['raw_text'])
        
        response = await self.reasoner.ainvoke(formatted_prompt)
        raw_response = response.content
        
        cleaned_response = re.sub(r"^\x60\x60\x60(?:markdown)?\s*", "", raw_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r"\s*\x60\x60\x60$", "", cleaned_response)
        
        return {"final_answer": cleaned_response.strip()}

    async def auditor_node(self, state: InsafState):
        answer = state["final_answer"]
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', answer) if len(s) > 20]
        context = "\n".join([p for p in state["precedents"]])

        prompt = ChatPromptTemplate.from_template(
            "Verify each sentence against the context. "
            "Return ONLY a JSON array of 1s and 0s (Length: {sentence_count}). "
            "1=Supported, 0=Unsupported. Example: [1, 0, 1]\n"
            "CONTEXT: {context}\nSENTENCES: {sentences}"
        )
        formatted_prompt = prompt.format(
            sentence_count=len(sentences), 
            context=context, 
            sentences=sentences
        )

        try:
            response = await self.fast_llm.ainvoke(formatted_prompt)
            raw_audit = str(response.content)
            match = re.search(r'\[(.*?)\]', raw_audit, re.S)

            if match:
                results = re.findall(r'\b[01]\b', match.group(1))
                if results:
                    supported = results.count('1')
                    score = supported / len(results)
                else:
                    score = 0.5
            else:
                score = 0.5
        except Exception as e:
            print(f"Auditor parse error: {e}")
            score = 0.5

        return {"audit_score": round(score, 2)}

def route_guardrail(state: InsafState):
    if state.get("is_valid", True):
        return "processor"
    return END

def build_async_graph(reasoner, fast_llm, vectorstore, reranker):
    nodes = AsyncGraphNodes(reasoner, fast_llm, vectorstore, reranker)
    builder = StateGraph(InsafState)
    
    builder.add_node("guardrail", nodes.guardrail_node)
    builder.add_node("processor", nodes.processor_node)
    builder.add_node("retriever", nodes.retriever_node)
    builder.add_node("reasoner", nodes.reasoner_node)
    builder.add_node("auditor", nodes.auditor_node)
    
    builder.add_edge(START, "guardrail")
    builder.add_conditional_edges("guardrail", route_guardrail)
    builder.add_edge("processor", "retriever")
    builder.add_edge("retriever", "reasoner")
    builder.add_edge("reasoner", "auditor")
    builder.add_edge("auditor", END)

    return builder.compile()