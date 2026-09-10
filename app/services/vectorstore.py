from qdrant_client import AsyncQdrantClient
from langchain_qdrant import QdrantVectorStore
from langchain_huggingface import HuggingFaceEmbeddings

def get_async_vectorstore(qdrant_url: str, qdrant_api_key: str) -> QdrantVectorStore:
    client = AsyncQdrantClient(
        url=qdrant_url,
        api_key=qdrant_api_key,
        prefer_grpc=True,
        timeout=15
    )
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        model_kwargs={'device': 'cpu'}
    )
    return QdrantVectorStore(
        client=client, 
        collection_name="pakistan_law", 
        embedding=embeddings
    )