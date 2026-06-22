"""
Knowledge Base for Triage (§10).

Builds and queries a FAISS index of break pattern documents for the
root-cause investigator node to retrieve relevant historical precedents.
"""

import json
import logging
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from src.utils.resilience import retry_with_backoff

import structlog

logger = structlog.get_logger(__name__)

KB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "knowledge_base"
INDEX_PATH = KB_PATH / "faiss_index"
DOCUMENTS_FILE = KB_PATH / "break_patterns.json"

_model = None


def _get_embedding_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def load_documents() -> list[dict]:
    with open(DOCUMENTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def build_index() -> tuple[faiss.IndexFlatL2, list[dict]]:
    """Build a FAISS index from the break pattern knowledge base."""
    documents = load_documents()
    model = _get_embedding_model()

    texts = [
        f"{doc['title']}. {doc['description']}. {doc['root_cause']}. {doc['resolution']}"
        for doc in documents
    ]
    embeddings = model.encode(texts, normalize_embeddings=True)

    dimension = embeddings.shape[1]
    index = faiss.IndexFlatL2(dimension)
    index.add(embeddings.astype(np.float32))

    INDEX_PATH.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_PATH / "index.faiss"))
    with open(INDEX_PATH / "documents.json", "w") as f:
        json.dump(documents, f)

    logger.info("knowledge_base.index_built", documents=len(documents), dimension=dimension)
    return index, documents


def load_index() -> tuple[faiss.IndexFlatL2, list[dict]]:
    """Load existing FAISS index, or build if not present."""
    index_file = INDEX_PATH / "index.faiss"
    docs_file = INDEX_PATH / "documents.json"

    if index_file.exists() and docs_file.exists():
        index = faiss.read_index(str(index_file))
        with open(docs_file, "r") as f:
            documents = json.load(f)
        return index, documents

    return build_index()


@retry_with_backoff(breaker_name="knowledge_base", max_attempts=2, base_delay=0.5)
def query_knowledge_base(
    query: str,
    top_k: int = 3,
    index: faiss.IndexFlatL2 | None = None,
    documents: list[dict] | None = None,
) -> list[dict]:
    """Query the knowledge base for relevant break patterns.

    Args:
        query: Natural language description of the break scenario
        top_k: Number of results to return

    Returns:
        List of relevant knowledge base documents with similarity scores
    """
    if index is None or documents is None:
        index, documents = load_index()

    model = _get_embedding_model()
    query_embedding = model.encode([query], normalize_embeddings=True)

    distances, indices = index.search(query_embedding.astype(np.float32), top_k)

    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < len(documents):
            doc = documents[idx].copy()
            doc["similarity_score"] = float(1 / (1 + dist))
            results.append(doc)

    return results
