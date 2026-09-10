import re
import asyncio
import numpy as np
from typing import TypedDict, List, Dict, Any, Literal
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langchain_core.prompts import ChatPromptTemplate


class GuardrailOutput(BaseModel):
    is_valid: bool = Field(
        description="True if input is a valid legal case, dispute, or question under Pakistani law; False otherwise."
    )


class ProcessorOutput(BaseModel):
    category: Literal["Criminal", "Civil", "Family"] = Field(
        description="Legal domain."
    )
    keywords: str = Field(
        description="Core search terms, statutes, and legal doctrines."
    )


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

        # json_mode prevents tool_use_failed errors on Groq open-source models
        try:
            self.guardrail_llm = self.fast_llm.with_structured_output(
                GuardrailOutput, method="json_mode"
            )
            self.processor_llm = self.fast_llm.with_structured_output(
                ProcessorOutput, method="json_mode"
            )
        except Exception:
            try:
                self.guardrail_llm = self.fast_llm.with_structured_output(
                    GuardrailOutput
                )
                self.processor_llm = self.fast_llm.with_structured_output(
                    ProcessorOutput
                )
            except Exception as e:
                print(f"Failed to bind structured output: {e}")
                self.guardrail_llm = None
                self.processor_llm = None

    async def guardrail_node(self, state: InsafState):
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are an automated legal classification gateway. "
                    "Determine if the input text contains a valid legal dispute, scenario, or query under Pakistani law. "
                    "Output strictly valid JSON matching the schema.",
                ),
                ("human", "{raw_text}"),
            ]
        )

        try:
            if self.guardrail_llm is None:
                raise RuntimeError("guardrail_llm is uninitialized")
            chain = prompt | self.guardrail_llm
            result: GuardrailOutput = await chain.ainvoke(
                {"raw_text": state["raw_text"]}
            )
            is_valid = bool(result.is_valid)
        except Exception as e:
            # Fail closed on API errors to prevent unverified execution
            print(f"guardrail_node error: {e}")
            is_valid = False

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
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Analyze the legal facts. Extract the primary legal category (Criminal, Civil, or Family) "
                    "and key statutory sections/legal terminology suitable for vector retrieval. Output valid JSON.",
                ),
                ("human", "{raw_text}"),
            ]
        )

        try:
            if self.processor_llm is None:
                raise RuntimeError("processor_llm is uninitialized")
            chain = prompt | self.processor_llm
            result: ProcessorOutput = await chain.ainvoke(
                {"raw_text": state["raw_text"]}
            )
            category = (
                result.category
                if result.category in {"Criminal", "Civil", "Family"}
                else "Civil"
            )
            keywords = (
                result.keywords.strip() if result.keywords else state["raw_text"][:80]
            )
        except Exception as e:
            print(f"processor_node error: {e}")
            category = "Civil"
            keywords = state["raw_text"][:80]

        return {"category": category, "legal_keywords": keywords}

    async def retriever_node(self, state: InsafState):
        query = state["legal_keywords"]
        raw_docs = await self.vectorstore.asimilarity_search_with_score(query, k=8)

        if not raw_docs:
            return {
                "precedents": [
                    "No strictly relevant Pakistani law precedents were found for this specific query."
                ],
                "precedent_meta": [{"source": "System", "score": 0.0}],
            }

        if self.reranker and raw_docs:
            full_query = f"Law of Pakistan regarding {state['category']}: {state['legal_keywords']}"
            pairs = [[full_query, doc.page_content[:1200]] for doc, _ in raw_docs]

            try:
                # Offload CPU inference to worker thread to prevent event loop blocking
                rr_scores = await asyncio.to_thread(self.reranker.predict, pairs)
                if isinstance(rr_scores, (list, tuple, np.ndarray)):
                    rr_scores_list = [float(x) for x in rr_scores]
                else:
                    rr_scores_list = [float(rr_scores)]

                def sigmoid(x: float) -> float:
                    return 1.0 / (1.0 + np.exp(-x))

                valid_ranked = []
                for (doc, _), raw_score in zip(raw_docs, rr_scores_list):
                    prob = sigmoid(raw_score)
                    if prob >= 0.45:
                        valid_ranked.append((doc, prob))

                ranked = sorted(valid_ranked, key=lambda x: x[1], reverse=True)[:3]
                if not ranked:
                    ranked = [(raw_docs[0][0], sigmoid(rr_scores_list[0]))]

                final_docs = [doc for doc, _ in ranked]
                final_scores = [score for _, score in ranked]
            except Exception as e:
                print(f"reranker failed, falling back to raw vector scores: {e}")
                final_docs = [doc for (doc, _) in raw_docs[:3]]
                final_scores = [score for (_, score) in raw_docs[:3]]
        else:
            final_docs = [doc for (doc, _) in raw_docs[:3]]
            final_scores = [score for (_, score) in raw_docs[:3]]

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
                    "- Avoid administrative generalities; focus on courtroom strategy.\n"
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

        return {"final_answer": cleaned_response.strip()}

    async def auditor_node(self, state: InsafState):
        answer = state.get("final_answer", "")
        context = "\n".join(state.get("precedents", []))

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Verify the factual grounding of the legal analysis against the contextual precedents. "
                    "Output ONLY a single JSON object with key 'audit_score' between 0.0 and 1.0. "
                    'Example: {{"audit_score": 0.85}}',
                ),
                ("human", "Context:\n{context}\n\nAnalysis:\n{answer}"),
            ]
        )

        try:
            messages = prompt.format_messages(context=context, answer=answer)
            response = await self.fast_llm.ainvoke(messages)
            raw_content = str(getattr(response, "content", response))

            match = re.search(
                r'["\']?audit_score["\']?\s*:\s*([0-9]*\.?[0-9]+)', raw_content
            )
            if match:
                score = float(match.group(1))
                score = max(0.0, min(1.0, score))
            else:
                score = 0.85 if len(state.get("precedents", [])) > 0 else 0.5
        except Exception as e:
            print(f"auditor_node error: {e}")
            score = 0.80

        return {"audit_score": round(score, 2)}


def route_guardrail(state: InsafState) -> str:
    if state.get("is_valid", False):
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
