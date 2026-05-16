from dotenv import load_dotenv
load_dotenv(".env")
import config, requests
print(f"OLLAMA_HOST: {config.OLLAMA_HOST}")
print(f"OLLAMA_MODEL: {config.OLLAMA_MODEL}")
try:
    r = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=5)
    tags = [m["name"] for m in r.json().get("models", [])]
    print(f"Ollama OK ({len(tags)} models): {tags}")
    print(f"Has extractor model? {any(config.OLLAMA_MODEL in t for t in tags)}")
    print(f"Has nomic-embed-text? {any('nomic-embed-text' in t for t in tags)}")
except Exception as e:
    print(f"Ollama unreachable: {e}")
try:
    from qdrant_client import QdrantClient
    qhost = getattr(config, "QDRANT_HOST", "localhost")
    qport = getattr(config, "QDRANT_PORT", 6333)
    qc = QdrantClient(host=qhost, port=qport, timeout=5)
    cols = [c.name for c in qc.get_collections().collections]
    print(f"Qdrant OK at {qhost}:{qport}, collections: {cols}")
except Exception as e:
    print(f"Qdrant unreachable: {e}")
