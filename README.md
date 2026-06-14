---
title: InsafDost AI Backend
emoji: ⚖️
colorFrom: blue
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

<div align="center">

  # InsafDost AI Backend

  **Production API Gateway and multi-agent reasoning engine for enterprise-grade Pakistani legal analysis.**

  <p>
    <img alt="Hugging Face Space" src="https://img.shields.io/badge/Deployed_on-Hugging_Face-FFD21E?logo=huggingface&logoColor=black" />
    <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi" />
    <img alt="Python 3.11" src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" />
    <img alt="License" src="https://img.shields.io/badge/License-MIT-green.svg" />
  </p>
</div>

---

## 📖 Table of Contents
- [About the Project](#-about-the-project)
- [Key Features](#-key-features)
- [Tech Stack](#-tech-stack)
- [Getting Started](#-getting-started)
- [Usage](#-usage)
- [System Architecture](#-system-architecture)
- [Contributing](#-contributing)
- [License](#-license)

---

## 🚀 About the Project

The InsafDost AI Backend is a standalone server designed to process complex legal scenarios and formulate actionable litigation strategies based on Pakistani case law. Serving as the core intelligence layer, this API ingests raw case descriptions, extracts legal doctrines, and retrieves the most relevant precedents to generate highly accurate, court-ready outputs.

This system is built for high reliability and deployed continuously via GitHub Actions to Hugging Face Spaces. It uses a graph-based state machine to ensure every legal query passes through strict guardrails, vector retrieval, cross-encoder reranking, and an automated factual audit before returning a response.

### 🎯 Key Features
* **LangGraph Reasoning Pipeline:** A robust, multi-stage state graph featuring Guardrail, Processor, Retriever, Reasoner, and Auditor nodes.
* **Advanced RAG Architecture:** Integrates Qdrant vector databases with `BAAI/bge-reranker-base` cross-encoders to fetch and rank the most contextually relevant legal precedents.
* **LLM Orchestration:** Leverages Groq's high-speed inference (Llama-3.3-70b and Llama-3.1-8b) to separate complex legal reasoning tasks from rapid structural classification.
* **Automated Hallucination Auditing:** Includes a dedicated auditor node that scores the final litigation strategy against the retrieved source texts to ensure factual grounding.

---

## 🛠 Tech Stack

* **API Framework:** FastAPI, Uvicorn, Pydantic
* **AI & Orchestration:** LangChain, LangGraph, Groq API
* **Embeddings & Retrieval:** Qdrant Vector Store, Hugging Face `all-MiniLM-L6-v2`, Sentence Transformers
* **Infrastructure:** Docker, Hugging Face Spaces, GitHub Actions (CI/CD)

---

## ⚙️ Getting Started

Follow these steps to set up the InsafDost backend locally.

### Prerequisites
* Python 3.11 (Recommended for maximum ML library stability)
* Access to [Groq API](https://console.groq.com/)
* A [Qdrant Database](https://qdrant.tech/) instance 

### Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/Abdurrafay19/insaf_dost_backend.git
   ```

2. Navigate to the project directory:
   ```bash
   cd insaf_dost_backend
   ```

3. Install dependencies:
   ```bash
   pip install --no-cache-dir -r requirements.txt
   ```

4. Set up environment variables by creating a `.env` file in the root directory:
   ```env
   GROQ_API_KEY=your_groq_api_key_here
   QDRANT_URL=your_qdrant_cluster_url
   QDRANT_API_KEY=your_qdrant_api_key
   ```

5. Run the application:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 7860 --reload
   ```



---

## 💡 Usage

Once the server is running (and the AI models have finished loading into memory), you can interact with the API via standard HTTP requests.

**Health Check Endpoint**

```bash
curl -X GET http://localhost:7860/health
```

**Analyze Case Endpoint**

```bash
curl -X POST http://localhost:7860/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "cases": [
      "A tenant has refused to pay rent for 6 months and is refusing to vacate the commercial property in Lahore despite multiple legal notices."
    ]
  }'
```

**Expected JSON Response:**

```json
{
  "status": "success",
  "data": [
    {
      "is_valid": true,
      "category": "Civil",
      "legal_keywords": "eviction commercial property rent default tenant",
      "final_answer": "### 1. Core Legal Issue\n...",
      "audit_score": 0.95,
      "_case_num": 1
    }
  ]
}
```

---

## 🏗 System Architecture

The application workflow follows a directed acyclic graph (DAG) defined in LangGraph:

1. **Guardrail Node:** Validates if the input is a legitimate legal query.
2. **Processor Node:** Extracts the legal category (Civil, Criminal, Family) and core search doctrines.
3. **Retriever Node:** Executes a semantic search in Qdrant, followed by a reranking pass to isolate the top 3 authoritative precedents.
4. **Reasoner Node:** Generates the comprehensive, Markdown-formatted litigation strategy using the Llama-3.3-70b model.
5. **Auditor Node:** Cross-references the generated strategy against the retrieved texts to output an accuracy `audit_score`.

---

## 🤝 Contributing

Contributions are what make the open-source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.