import logging
import math
import threading
from typing import List, Dict

logger = logging.getLogger(__name__)

# ── Reranking model (loaded once on demand) ────────────────────────────────────
_reranker_model = None
_reranker_lock = threading.Lock()

def _get_reranker_model():
    global _reranker_model
    if _reranker_model is None:
        with _reranker_lock:
            if _reranker_model is None:
                from sentence_transformers import CrossEncoder
                logger.info("Loading cross-encoder/ms-marco-MiniLM-L-6-v2 reranker model on demand (~80MB, cached on disk)...")
                # Enforce CPU execution to ensure compatibility inside the backend container
                _reranker_model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", device="cpu")
                logger.info("Reranker model ready.")
    return _reranker_model


def sigmoid(x: float) -> float:
    """Map raw logits from Cross-Encoder to [0, 1] range."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def rerank_hits(query: str, hits: List[Dict], top_k: int = 5) -> List[Dict]:
    """
    Rerank a list of search hits using the cross-encoder model.
    
    Each hit in hits is expected to be a dict:
        {"id": ..., "text": ..., "metadata": ..., "score": ...}
        
    Returns the top_k hits, sorted by cross-encoder score descending.
    The "score" field is overwritten with the normalized sigmoid score,
    and the original Milvus score is kept as "milvus_score".
    """
    if not hits:
        return []

    model = _get_reranker_model()
    pairs = [[query, hit["text"]] for hit in hits]
    
    # model.predict returns numpy array or list of floats
    raw_scores = model.predict(pairs)
    
    # Update hits with new scores
    for hit, raw_score in zip(hits, raw_scores):
        hit["milvus_score"] = hit.get("score", 0.0)
        hit["score"] = round(sigmoid(float(raw_score)), 4)
        
    # Sort descending by the new normalized score
    sorted_hits = sorted(hits, key=lambda x: x["score"], reverse=True)
    return sorted_hits[:top_k]
