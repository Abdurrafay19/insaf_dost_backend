import os
from qdrant_client import QdrantClient

def heartbeat():
    url = os.environ.get("QDRANT_URL")
    api_key = os.environ.get("QDRANT_API_KEY")
    
    if not url or not api_key:
        print("Missing environment variables. Check your Secrets.")
        return

    try:
        client = QdrantClient(url=url, api_key=api_key)
        collections = client.get_collections()
        print(f"Successfully pinged Qdrant. Found {len(collections.collections)} collections.")
    except Exception as e:
        print(f"Failed to ping Qdrant: {e}")

if __name__ == "__main__":
    heartbeat()