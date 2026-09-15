import os
import sys
import json
import time
import asyncio
import datetime
from pathlib import Path
import numpy as np
from sklearn.metrics import precision_score, recall_score, f1_score
from sentence_transformers import CrossEncoder
from langchain_groq import ChatGroq

from app.core.config import get_settings
from app.services.vectorstore import get_async_vectorstore
from app.workflows.graph import AsyncGraphNodes

from benchmarks.dataset import (
    REALISTIC_RETRIEVAL_TESTBED,
    GUARDRAIL_TESTBED,
    AUDITOR_TESTBED,
)

settings = get_settings()



# =====================================================================
# UNIFIED BENCHMARK EXECUTION HARNESS
# =====================================================================
async def execute_unified_benchmark():
    wall_start = time.perf_counter()
    timestamp_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    print("=" * 70)
    print(f"INSAF DOST PRODUCTION BENCHMARK SUITE - EXECUTION START")
    print(f"Timestamp: {timestamp_str}")
    print("=" * 70)

    # -----------------------------------------------------------------
    # PHASE 1: RETRIEVAL ABLATION (N = 200, Local CPU + Qdrant, 0 Tokens)
    # -----------------------------------------------------------------
    print("\n[PHASE 1/3] Executing Realistic Retrieval Benchmark (N=200, 0 Groq Tokens)...")
    vectorstore = get_async_vectorstore(settings.qdrant_url, settings.qdrant_api_key)
    reranker = CrossEncoder("BAAI/bge-reranker-base", max_length=512, device="cpu")

    dense_h1, dense_h3, dense_h5 = [], [], []
    rerank_h1, rerank_h3, rerank_h5 = [], [], []
    dense_rr3, rerank_rr3 = [], []
    retrieval_latencies_ms = []
    retrieval_records = []

    for idx, item in enumerate(REALISTIC_RETRIEVAL_TESTBED):
        t0 = time.perf_counter()
        raw_docs = await vectorstore.asimilarity_search_with_score(item["query"], k=8)
        dense_texts = [d.page_content.lower() for d, _ in raw_docs]

        full_query = f"Law of Pakistan regarding {item['category']}: {item['query']}"
        pairs = [[full_query, d.page_content[:1200]] for d, _ in raw_docs]
        scores = reranker.predict(pairs)

        sigmoid_scores = 1.0 / (1.0 + np.exp(-np.array(scores)))
        ranked = [
            (doc.page_content.lower(), score)
            for (doc, _), score in zip(raw_docs, sigmoid_scores)
            if score >= 0.40
        ]
        ranked.sort(key=lambda x: x[1], reverse=True)
        rerank_texts = [t for t, _ in ranked] if ranked else dense_texts[:3]
        elapsed_ms = (time.perf_counter() - t0) * 1000
        retrieval_latencies_ms.append(elapsed_ms)

        target = item["target"].lower()
        dh1 = 1 if any(target in t for t in dense_texts[:1]) else 0
        dh3 = 1 if any(target in t for t in dense_texts[:3]) else 0
        dh5 = 1 if any(target in t for t in dense_texts[:5]) else 0
        dr3 = next((i + 1 for i, t in enumerate(dense_texts[:3]) if target in t), 0)

        rh1 = 1 if any(target in t for t in rerank_texts[:1]) else 0
        rh3 = 1 if any(target in t for t in rerank_texts[:3]) else 0
        rh5 = 1 if any(target in t for t in rerank_texts[:5]) else 0
        rr3 = next((i + 1 for i, t in enumerate(rerank_texts[:3]) if target in t), 0)

        dense_h1.append(dh1); dense_h3.append(dh3); dense_h5.append(dh5)
        rerank_h1.append(rh1); rerank_h3.append(rh3); rerank_h5.append(rh5)
        dense_rr3.append(1.0 / dr3 if dr3 > 0 else 0.0)
        rerank_rr3.append(1.0 / rr3 if rr3 > 0 else 0.0)

        retrieval_records.append({
            "query_id": idx + 1,
            "category": item["category"],
            "target": item["target"],
            "latency_ms": round(elapsed_ms, 2),
            "dense_hit3": bool(dh3),
            "rerank_hit3": bool(rh3),
            "dense_rank": dr3,
            "rerank_rank": rr3
        })

        if (idx + 1) % 15 == 0:
            print(f"  -> Processed {idx+1:02d}/200 queries | Current Reranked Hit@3: {np.mean(rerank_h3)*100:.1f}%")

    retrieval_summary = {
        "sample_size": len(REALISTIC_RETRIEVAL_TESTBED),
        "mean_latency_ms": round(float(np.mean(retrieval_latencies_ms)), 2),
        "p95_latency_ms": round(float(np.percentile(retrieval_latencies_ms, 95)), 2),
        "metrics": {
            "dense_hit1": round(float(np.mean(dense_h1)), 3),
            "dense_hit3": round(float(np.mean(dense_h3)), 3),
            "dense_hit5": round(float(np.mean(dense_h5)), 3),
            "dense_mrr3": round(float(np.mean(dense_rr3)), 3),
            "rerank_hit1": round(float(np.mean(rerank_h1)), 3),
            "rerank_hit3": round(float(np.mean(rerank_h3)), 3),
            "rerank_hit5": round(float(np.mean(rerank_h5)), 3),
            "rerank_mrr3": round(float(np.mean(rerank_rr3)), 3),
            "hit3_absolute_delta": round(float(np.mean(rerank_h3) - np.mean(dense_h3)), 3),
            "mrr3_absolute_delta": round(float(np.mean(rerank_rr3) - np.mean(dense_rr3)), 3),
        },
        "query_traces": retrieval_records
    }
    print(f"Phase 1 Finished. Hit@3 Delta: {retrieval_summary['metrics']['hit3_absolute_delta']*100:+.1f}% | MRR@3 Delta: {retrieval_summary['metrics']['mrr3_absolute_delta']:+.3f}")

    # -----------------------------------------------------------------
    # PHASE 2: GUARDRAIL EVALUATION (N = 100, gpt-oss-20b, Paced 2.0s)
    # -----------------------------------------------------------------
    print("\n[PHASE 2/3] Executing Guardrail Benchmark (N=100, gpt-oss-20b, Paced 2.0s)...")
    fast_llm = ChatGroq(model="openai/gpt-oss-20b", temperature=0.0, api_key=settings.groq_api_key, max_retries=2)
    nodes = AsyncGraphNodes(reasoner=None, fast_llm=fast_llm, vectorstore=None, reranker=None)

    g_true, g_pred = [], []
    guardrail_latencies_ms = []
    guardrail_records = []

    for idx, item in enumerate(GUARDRAIL_TESTBED):
        t0 = time.perf_counter()
        state = {"raw_text": item["text"]}
        res = await nodes.guardrail_node(state)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        guardrail_latencies_ms.append(elapsed_ms)

        predicted = bool(res.get("is_valid", False))
        g_true.append(item["expected"])
        g_pred.append(predicted)

        guardrail_records.append({
            "case_id": idx + 1,
            "text": item["text"],
            "expected": item["expected"],
            "predicted": predicted,
            "latency_ms": round(elapsed_ms, 2),
            "passed": predicted == item["expected"]
        })
        print(f"  -> Case [{idx+1:02d}/100] | Expected: {str(item['expected']):<5} | Predicted: {str(predicted):<5} | {'OK' if predicted == item['expected'] else 'FAIL'}")
        
        if idx < len(GUARDRAIL_TESTBED) - 1:
            await asyncio.sleep(2.0)  # Pacing to protect 30 RPM cap

    p = precision_score(g_true, g_pred, zero_division=0)
    r = recall_score(g_true, g_pred, zero_division=0)
    f1 = f1_score(g_true, g_pred, zero_division=0)

    guardrail_summary = {
        "sample_size": len(GUARDRAIL_TESTBED),
        "mean_latency_ms": round(float(np.mean(guardrail_latencies_ms)), 2),
        "p95_latency_ms": round(float(np.percentile(guardrail_latencies_ms, 95)), 2),
        "metrics": {
            "precision": round(float(p), 3),
            "recall": round(float(r), 3),
            "f1_score": round(float(f1), 3),
            "total_passed": int(sum(1 for r in guardrail_records if r["passed"])),
            "accuracy": round(float(sum(1 for r in guardrail_records if r["passed"]) / len(GUARDRAIL_TESTBED)), 3)
        },
        "case_traces": guardrail_records
    }
    print(f"Phase 2 Finished. Precision: {p:.3f} | Recall: {r:.3f} | F1: {f1:.3f}")

    # -----------------------------------------------------------------
    # PHASE 3: AUDITOR GROUNDING BENCHMARK (N = 50, gpt-oss-20b, Paced 12.5s)
    # -----------------------------------------------------------------
    print("\n[PHASE 3/3] Executing Auditor Benchmark (N=50, gpt-oss-20b, Paced 12.5s)...")
    auditor_latencies_ms = []
    auditor_records = []
    grounded_scores, hallucinated_scores = [], []

    for idx, item in enumerate(AUDITOR_TESTBED):
        t0 = time.perf_counter()
        state = {"final_answer": item["final_answer"], "precedents": item["precedents"]}
        res = await nodes.auditor_node(state)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        auditor_latencies_ms.append(elapsed_ms)

        score = float(res.get("audit_score", 0.0))
        if item["expected_grounded"]:
            grounded_scores.append(score)
        else:
            hallucinated_scores.append(score)

        auditor_records.append({
            "test_id": idx + 1,
            "test_name": item["name"],
            "expected_grounded": item["expected_grounded"],
            "score": score,
            "latency_ms": round(elapsed_ms, 2)
        })
        print(f"  -> Audit [{idx+1:02d}/50] {item['name']:<40} | Score: {score:.2f} | Latency: {elapsed_ms:.0f}ms")

        if idx < len(AUDITOR_TESTBED) - 1:
            print("     Sleeping 12.5s to enforce safe Groq Free Tier TPM boundaries...")
            await asyncio.sleep(12.5)

    mean_grounded = float(np.mean(grounded_scores)) if grounded_scores else 0.0
    mean_hallucinated = float(np.mean(hallucinated_scores)) if hallucinated_scores else 0.0
    discrimination_gap = mean_grounded - mean_hallucinated

    auditor_summary = {
        "sample_size": len(AUDITOR_TESTBED),
        "mean_latency_ms": round(float(np.mean(auditor_latencies_ms)), 2),
        "p95_latency_ms": round(float(np.percentile(auditor_latencies_ms, 95)), 2),
        "metrics": {
            "mean_grounded_score": round(mean_grounded, 3),
            "mean_hallucinated_score": round(mean_hallucinated, 3),
            "discrimination_gap": round(discrimination_gap, 3),
            "hallucination_rejection_rate": round(float(sum(1 for s in hallucinated_scores if s <= 0.40) / len(hallucinated_scores)), 3)
        },
        "audit_traces": auditor_records
    }
    print(f"Phase 3 Finished. Grounded Mean: {mean_grounded:.2f} | Hallucinated Mean: {mean_hallucinated:.2f} | Gap: {discrimination_gap:.2f}")

    # -----------------------------------------------------------------
    # COMPILATION & ATOMIC JSON EXPORT
    # -----------------------------------------------------------------
    wall_duration = time.perf_counter() - wall_start
    final_output = {
        "metadata": {
            "benchmark_suite_version": "2.1.0",
            "execution_timestamp_utc": timestamp_str,
            "total_wall_clock_time_seconds": round(wall_duration, 2),
            "environment": {
                "reranker_model": "BAAI/bge-reranker-base",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "classification_model": "openai/gpt-oss-20b",
                "target_vector_db": "Qdrant Cloud (collection: pakistan_law)"
            }
        },
        "retrieval_benchmark": retrieval_summary,
        "guardrail_benchmark": guardrail_summary,
        "auditor_benchmark": auditor_summary
    }

    out_file = Path(__file__).resolve().parent / "results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 70)
    print(f"[COMPLETE] Benchmark run finished in {wall_duration:.1f}s.")
    print(f"[OUTPUT] Results logged to: {out_file}")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(execute_unified_benchmark())