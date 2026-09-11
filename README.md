---
title: InsafDost AI Backend
emoji: ⚖️
colorFrom: blue
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

# InsafDost AI Backend

Asynchronous REST API and state-graph execution engine for automated Pakistani legal reasoning, precedent retrieval from Qdrant, and factual consistency auditing.

---

## Technical Overview

The application processes legal scenarios through an asynchronous LangGraph execution pipeline. Inbound text is validated through a fail-closed classification guardrail, categorized into civil, criminal, or family jurisdictions, queried against a dense vector store of Pakistani case law, reranked via a cross-encoder, synthesized into an appellate litigation strategy, and factually audited before response serialization.

```text
Client Request
      │
      ▼
┌──────────────┐
│  /analyze    │  FastAPI (Sequential batch processing with token backoff)
└──────┬───────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────┐
│ LangGraph State Machine (InsafState)                             │
│                                                                  │
│  [guardrail] ──(is_valid=False)──► END (Rejection payload)       │
│        │                                                         │
│   (is_valid=True)                                                │
│        ▼                                                         │
│  [processor] ──► Extracts category and statutory search terms    │
│        │                                                         │
│        ▼                                                         │
│  [retriever] ──► Qdrant ANN search (k=8) + BGE cross-encoder     │
│        │         (Thread-offloaded CPU inference with sigmoid)   │
│        ▼                                                         │
│  [reasoner]  ──► Groq openai/gpt-oss-120b legal synthesis        │
│        │                                                         │
│        ▼                                                         │
│  [auditor]   ──► Groq openai/gpt-oss-20b grounding audit         │
│        │                                                         │
│        ▼                                                         │
│       END                                                        │
└──────────────────────────────────┬───────────────────────────────┘
                                   │
                                   ▼
                       Structured JSON Response

```

---

## System Architecture

* **Decoupled Lifecycle Initialization:** Model downloads run inside an `asyncio.create_task` during the FastAPI lifespan context. Liveness probes respond immediately during startup without triggering orchestration timeout terminations.
* **Fail-Closed Guardrails:** The guardrail node employs defensive JSON parsing with fallback inspection across boolean keys. If the classification model returns malformed data or fails, the pipeline sets `is_valid = False` and terminates progression.
* **Two-Stage Retrieval Pipeline:** Performs approximate nearest-neighbor search against the Qdrant `pakistan_law` collection, followed by cross-encoder reranking via `BAAI/bge-reranker-base`. Cross-encoder matrix calculations run in a worker thread via `asyncio.to_thread` to prevent event loop blocking. Logits are mapped to probabilities via sigmoid activation and filtered at a calibrated threshold (`prob >= 0.40`).
* **Rate-Limit Pacing:** Batch requests to `/analyze` execute sequentially with a 2-second inter-case buffer and exponential backoff retry handling upon receiving HTTP 429 status codes from Groq.
* **Markdown Formatting Constraints:** The reasoning prompt restricts Markdown tables and pipe characters (`|`), mandating bulleted lists to prevent frontend parsing failures.

---

## Directory Structure

```text
InsafDostBackend/
├── app/
│   ├── core/
│   │   ├── __init__.py
│   │   └── config.py          # Pydantic BaseSettings environment validation
│   ├── services/
│   │   ├── __init__.py
│   │   └── vectorstore.py     # Qdrant client connection and HuggingFaceEmbeddings
│   ├── workflows/
│   │   ├── __init__.py
│   │   └── graph.py           # LangGraph StateGraph, node logic, and routing
│   ├── __init__.py
│   └── main.py                # FastAPI app, lifespan handler, CORS, and endpoints
├── .dockerignore
├── .env.example
├── .gitattributes
├── .gitignore
├── CODE_OF_CONDUCT.md
├── Dockerfile                 # Container definition targeting Python 3.11-slim
├── LICENSE.md
├── ping_qdrant.py             # Qdrant cluster connectivity utility
├── README.md
└── requirements.txt           # Pinned Python dependencies

```

---

## Dependencies and Runtime

| Component | Technology | Version / Target |
| --- | --- | --- |
| Runtime | Python | 3.11-slim |
| Web Framework | FastAPI | 0.136.1 |
| ASGI Server | Uvicorn | 0.46.0 |
| Workflow Engine | LangGraph | 1.1.10 |
| Legal Reasoning LLM | Groq API (`openai/gpt-oss-120b`) | Temperature 0.0 |
| Classification LLM | Groq API (`openai/gpt-oss-20b`) | Temperature 0.0 |
| Vector Store | Qdrant Cloud | Client 1.17.1 |
| Embedding Model | `sentence-transformers/all-MiniLM-L6-v2` | CPU |
| Cross-Encoder Reranker | `BAAI/bge-reranker-base` | CPU (512 max length) |
| Configuration Management | Pydantic Settings | 2.15.0 |

---

## Prerequisites

* Python 3.11
* Active Qdrant Cloud cluster with an initialized `pakistan_law` collection
* Groq API account with active API credentials

---

## Configuration

The service reads configuration values from environment variables via `app/core/config.py`.

Create a `.env` file in the project root:

```bash
cp .env.example .env

```

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `GROQ_API_KEY` | string | None | Authorization key for Groq Cloud API endpoints. |
| `QDRANT_URL` | string | None | HTTPS endpoint URL of the Qdrant cluster. |
| `QDRANT_API_KEY` | string | None | API key for Qdrant Cloud authentication. |

---

## Installation and Local Setup

1. Clone the repository:

```bash
git clone https://github.com/Abdurrafay19/insaf_dost_backend.git
cd insaf_dost_backend

```

1. Create and activate a Python 3.11 virtual environment:

```bash
python3.11 -m venv venv
source venv/bin/activate

```

1. Install dependencies:

```bash
pip install --no-cache-dir -r requirements.txt

```

1. Verify Qdrant connectivity:

```bash
python ping_qdrant.py

```

1. Run the development server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 7860 --reload

```

---

## API Reference

### Liveness Probe

Verifies that the ASGI web server is responsive.

```bash
curl -X GET http://localhost:7860/health

```

Expected response (`200 OK`):

```json
{
  "status": "healthy",
  "service": "InsafDost AI Gateway"
}

```

### Readiness Probe

Verifies whether model weights are loaded and the LangGraph pipeline is compiled.

```bash
curl -X GET http://localhost:7860/ready

```

Expected response when initialized (`200 OK`):

```json
{
  "status": "ready"
}

```

Expected response while loading (`503 Service Unavailable`):

```json
{
  "detail": "AI models are still loading."
}

```

### Case Analysis

Processes a list of legal scenario texts through the classification, retrieval, reasoning, and auditing pipeline.

```bash
curl -X POST http://localhost:7860/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "cases": [
      "A shopkeeper in Karachi used tampered weighing scales to shortchange customers and threatened physical violence when confronted."
    ]
  }'

```

Expected response (`200 OK`):

```json
{
  "status": "success",
  "data": [
    {
      "is_valid": true,
      "category": "Criminal",
      "legal_keywords": "Pakistan Penal Code Section 264 265 fraudulent weights Section 506 criminal intimidation",
      "precedents": [
        "Criminal Revision No. 412: In cases concerning fraudulent weights under Section 264 PPC..."
      ],
      "precedent_meta": [
        {
          "source": "Court Precedent",
          "score": 0.871
        }
      ],
      "final_answer": "### 1. Core Legal Issue\n\nWhether the use of fraudulent balance scales constitutes an offense under Section 264/265 of the Pakistan Penal Code (PPC) and whether verbal threats constitute criminal intimidation under Section 506 PPC.\n\n### 2. Applicable Law & Precedents\n\n- [1] Criminal Revision No. 412 - Establishes evidentiary requirements for seizing weighing mechanisms.\n\n### 3. Case Analysis\n\nThe accused intentionally employed tampered measurement devices...\n\n### 4. Actionable Litigation Strategy\n\n- Forum: Judicial Magistrate 1st Class having local territorial jurisdiction.\n- Application: File formal complaint under Section 190 CrPC or direct FIR registration under Section 154 CrPC for cognizable offenses.\n- Evidence Required: Seizure memo of the scale verified by Inspector of Weights and Measures.",
      "audit_score": 0.92,
      "_case_num": 1
    }
  ]
}

```

---

## Container Deployment

The application includes a `Dockerfile` targeting containerized platforms and Hugging Face Spaces using non-root execution on port `7860`.

Build the image:

```bash
docker build -t insafdost-backend:latest .

```

Run the container:

```bash
docker run -d \
  --name insafdost-service \
  -p 7860:7860 \
  --env-file .env \
  insafdost-backend:latest

```

---

## License

Distributed under the MIT License. See `LICENSE.md` for details.
