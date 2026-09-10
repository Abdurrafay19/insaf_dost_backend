import re
import json
import asyncio
import numpy as np
from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, START, END
from langchain_core.prompts import ChatPromptTemplate


class InsafState(TypedDict):
    raw_text: str
    is_valid: bool
    category: str
    legal_keywords: str
    precedents: List[str]
    precedent_meta: List[Dict[str, Any]]
    final_answer: str
    audit_score: float


class AsyncGraphNodes:
    def __init__(self, reasoner, fast_llm, vectorstore, reranker):
        self.reasoner = reasoner
        self.fast_llm = fast_llm
        self.vectorstore = vectorstore
        self.reranker = reranker

    async def guardrail_node(self, state: InsafState):
        text_preview = state["raw_text"][:60].replace("\n", " ")
        print(f"\n[TRACE] >>> guardrail_node started: '{text_preview}...'")

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are an automated legal classification gateway. Determine if the text describes a legal issue, "
                    "crime, commercial dispute, family dispute, or legal question under Pakistani law.\n"
                    "Output strictly valid JSON with this exact key:\n"
                    '{{"is_valid": true}} OR {{"is_valid": false}}',
                ),
                ("human", "{raw_text}"),
            ]
        )

        messages = prompt.format_messages(raw_text=state["raw_text"])
        is_valid = False

        try:
            response = await self.fast_llm.ainvoke(messages)
            raw_content = str(getattr(response, "content", response)).strip()
            print(f"[TRACE] guardrail raw response: {raw_content}")

            # Strip possible markdown code fences
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_content, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()

            # Extract json block
            match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
            if match:
                payload = json.loads(match.group(0))
                # Check target key, followed by known fallback aliases
                if "is_valid" in payload:
                    is_valid = bool(payload["is_valid"])
                elif "valid_dispute" in payload:
                    is_valid = bool(payload["valid_dispute"])
                elif "isLegalDispute" in payload:
                    is_valid = bool(payload["isLegalDispute"])
                elif "contains_legal_dispute" in payload:
                    is_valid = bool(payload["contains_legal_dispute"])
                elif "isLegalScenario" in payload:
                    is_valid = bool(payload["isLegalScenario"])
                else:
                    # Fallback check on truthy values in any returned boolean key
                    is_valid = any(
                        v is True for v in payload.values() if isinstance(v, bool)
                    )
            else:
                is_valid = "true" in raw_content.lower()

        except Exception as e:
            print(f"[TRACE] guardrail_node exception: {e}")
            is_valid = False

        print(f"[TRACE] guardrail verdict: is_valid={is_valid}")

        if not is_valid:
            return {
                "is_valid": False,
                "category": "Irrelevant",
                "legal_keywords": "",
                "final_answer": "This does not appear to be a valid legal scenario. Please provide a relevant legal case.",
                "audit_score": 1.0,
                "precedents": [],
                "precedent_meta": [],
            }

        return {"is_valid": True}

    async def processor_node(self, state: InsafState):
        print(f"[TRACE] >>> processor_node started")
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Analyze the legal facts. Extract the primary legal category (Criminal, Civil, or Family) "
                    "and key statutory sections/legal terminology suitable for vector retrieval.\n"
                    "Output strictly valid JSON with these exact keys:\n"
                    '{{"category": "Criminal", "keywords": "Pakistan Penal Code Section 411 theft possession"}}',
                ),
                ("human", "{raw_text}"),
            ]
        )

        messages = prompt.format_messages(raw_text=state["raw_text"])
        category = "Civil"
        keywords = state["raw_text"][:80]

        try:
            response = await self.fast_llm.ainvoke(messages)
            raw_content = str(getattr(response, "content", response)).strip()
            print(f"[TRACE] processor raw response: {raw_content}")

            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_content, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()

            match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
            if match:
                payload = json.loads(match.group(0))
                parsed_cat = payload.get("category", "")
                if parsed_cat in {"Criminal", "Civil", "Family"}:
                    category = parsed_cat
                keywords = payload.get("keywords", keywords)
        except Exception as e:
            print(f"[TRACE] processor_node exception: {e}")

        print(f"[TRACE] processor parsed: category={category}, keywords={keywords}")
        return {"category": category, "legal_keywords": keywords}

    async def retriever_node(self, state: InsafState):
        query = state["legal_keywords"]
        print(f"[TRACE] >>> retriever_node querying Qdrant with: '{query}'")

        try:
            raw_docs = await self.vectorstore.asimilarity_search_with_score(query, k=8)
            print(f"[TRACE] Qdrant returned {len(raw_docs)} documents")
        except Exception as e:
            print(f"[TRACE] Qdrant query error: {e}")
            raw_docs = []

        if not raw_docs:
            print("[TRACE] No documents retrieved. Exiting retriever.")
            return {
                "precedents": [
                    "No strictly relevant Pakistani law precedents were found for this query."
                ],
                "precedent_meta": [{"source": "System", "score": 0.0}],
            }

        if self.reranker and raw_docs:
            full_query = f"Law of Pakistan regarding {state['category']}: {state['legal_keywords']}"
            pairs = [[full_query, doc.page_content[:1200]] for doc, _ in raw_docs]
            print(f"[TRACE] Running reranker on {len(pairs)} candidate pairs...")

            try:
                rr_scores = await asyncio.to_thread(self.reranker.predict, pairs)
                scores_list = [
                    float(x)
                    for x in (
                        rr_scores
                        if isinstance(rr_scores, (list, tuple, np.ndarray))
                        else [rr_scores]
                    )
                ]

                def sigmoid(x: float) -> float:
                    return 1.0 / (1.0 + np.exp(-x))

                valid_ranked = []
                for (doc, _), raw_score in zip(raw_docs, scores_list):
                    prob = sigmoid(raw_score)
                    if prob >= 0.40:
                        valid_ranked.append((doc, prob))

                ranked = sorted(valid_ranked, key=lambda x: x[1], reverse=True)[:3]
                if not ranked:
                    ranked = [(raw_docs[0][0], sigmoid(scores_list[0]))]

                final_docs = [doc for doc, _ in ranked]
                final_scores = [score for _, score in ranked]
            except Exception as e:
                print(
                    f"[TRACE] Reranker exception: {e}. Falling back to top 3 vector hits."
                )
                final_docs = [doc for (doc, _) in raw_docs[:3]]
                final_scores = [score for (_, score) in raw_docs[:3]]
        else:
            final_docs = [doc for (doc, _) in raw_docs[:3]]
            final_scores = [score for (_, score) in raw_docs[:3]]

        print(f"[TRACE] retriever completed with {len(final_docs)} selected precedents")
        return {
            "precedents": [doc.page_content for doc in final_docs],
            "precedent_meta": [
                {
                    "source": doc.metadata.get("source", "Court Precedent"),
                    "score": round(float(score), 3),
                }
                for doc, score in zip(final_docs, final_scores)
            ],
        }

    async def reasoner_node(self, state: InsafState):
        print(f"[TRACE] >>> reasoner_node generating legal opinion...")
        precedents = state.get("precedents", [])
        precedent_meta = state.get("precedent_meta", [])

        context_lines = []
        for i, p in enumerate(precedents):
            meta_item = precedent_meta[i] if i < len(precedent_meta) else {}
            source = (
                meta_item.get("source", "Court Precedent")
                if isinstance(meta_item, dict)
                else "Court Precedent"
            )
            context_lines.append(f"[{i+1}] Authority: {source}\n{p}")
        context = "\n\n".join(context_lines)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Act as a senior litigator in Pakistan. Using ONLY the provided authorities, analyze the case facts.\n\n"
                    "Authorities:\n{context}\n\n"
                    "Structure requirements:\n"
                    "### 1. Core Legal Issue\n\n"
                    "### 2. Applicable Law & Precedents (Use [Number] citations)\n\n"
                    "### 3. Case Analysis\n\n"
                    "### 4. Actionable Litigation Strategy\n\n"
                    "Rules:\n"
                    "- In Section 4, state exact court forums, specific petitions/applications, and evidentiary requirements.\n"
                    "- Avoid administrative generalities; focus strictly on litigation.\n"
                    "- Maintain clean newlines between headers and sections.",
                ),
                ("human", "Case Facts:\n{raw_text}"),
            ]
        )

        messages = prompt.format_messages(context=context, raw_text=state["raw_text"])
        response = await self.reasoner.ainvoke(messages)
        raw_response = str(getattr(response, "content", response))

        cleaned_response = re.sub(
            r"^\x60\x60\x60(?:markdown)?\s*", "", raw_response, flags=re.IGNORECASE
        )
        cleaned_response = re.sub(r"\s*\x60\x60\x60$", "", cleaned_response)
        print(f"[TRACE] reasoner generated {len(cleaned_response)} characters")

        return {"final_answer": cleaned_response.strip()}

    async def auditor_node(self, state: InsafState):
        print(f"[TRACE] >>> auditor_node verifying grounding...")
        answer = state.get("final_answer", "")
        context = "\n".join(state.get("precedents", []))

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Verify the factual grounding of the legal analysis against the contextual precedents. "
                    "Output strictly valid JSON with key 'audit_score' between 0.0 and 1.0.\n"
                    'Example: {{"audit_score": 0.85}}',
                ),
                ("human", "Context:\n{context}\n\nAnalysis:\n{answer}"),
            ]
        )

        score = 0.85 if len(state.get("precedents", [])) > 0 else 0.5
        try:
            messages = prompt.format_messages(context=context, answer=answer)
            response = await self.fast_llm.ainvoke(messages)
            raw_content = str(getattr(response, "content", response))
            print(f"[TRACE] auditor raw response: {raw_content}")

            match = re.search(
                r'["\']?audit_score["\']?\s*:\s*([0-9]*\.?[0-9]+)', raw_content
            )
            if match:
                parsed_val = float(match.group(1))
                score = max(0.0, min(1.0, parsed_val))
        except Exception as e:
            print(f"[TRACE] auditor_node error: {e}")

        print(f"[TRACE] auditor final score: {score}")
        return {"audit_score": round(score, 2)}


def route_guardrail(state: InsafState) -> str:
    verdict = state.get("is_valid", False)
    print(f"[TRACE] route_guardrail -> {'processor' if verdict else 'END'}")
    return "processor" if verdict else END


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
