import asyncio
import sys
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.reranker import sigmoid, rerank_hits
from app.services.evaluation import _search_one_requirement


def test_sigmoid():
    """Verify sigmoid mapping of logits to [0, 1] range."""
    assert round(sigmoid(0.0), 2) == 0.50
    assert sigmoid(100.0) == pytest.approx(1.0, abs=1e-5)
    assert sigmoid(-100.0) == pytest.approx(0.0, abs=1e-5)
    # Positive logits should map to high confidence scores
    assert sigmoid(2.2) > 0.90
    # Negative logits should map to low confidence scores
    assert sigmoid(-2.2) < 0.10


def test_rerank_hits():
    """Verify rerank_hits correctly scores, sorts, and limits hits."""
    hits = [
        {"id": "1", "text": "This is doc 1", "score": 0.3, "metadata": {}},
        {"id": "2", "text": "This is doc 2", "score": 0.4, "metadata": {}},
    ]
    
    # Mock CrossEncoder
    mock_encoder = MagicMock()
    # Mock prediction scores: doc 1 gets logit 3.0 (high), doc 2 gets logit -3.0 (low)
    mock_encoder.predict.return_value = [3.0, -3.0]
    
    with patch("app.services.reranker._get_reranker_model", return_value=mock_encoder):
        reranked = rerank_hits("query text", hits, top_k=2)
        
        # Verify sorting (doc 1 should be first because of logit 3.0)
        assert reranked[0]["id"] == "1"
        assert reranked[1]["id"] == "2"
        
        # Verify scores are converted using sigmoid
        assert reranked[0]["score"] > 0.90
        assert reranked[1]["score"] < 0.10
        
        # Verify milvus_score is preserved
        assert reranked[0]["milvus_score"] == 0.3
        assert reranked[1]["milvus_score"] == 0.4


@pytest.mark.asyncio
async def test_search_one_requirement_with_rerank():
    """Verify evaluation's _search_one_requirement retrieves 10 candidates and formats top 5."""
    req = {
        "req_id": "req-1",
        "req_code": "REQ-101",
        "description": "Encryption at rest requirement"
    }
    
    mock_vs = MagicMock()
    # Search returns 10 hits
    mock_vs.search = MagicMock(return_value=[
        {"id": f"hit-{i}", "text": f"evidence text {i}", "score": 0.5, "metadata": {"file_name": "doc.pdf", "page_number": i}}
        for i in range(1, 11)
    ])
    
    # Reranker returns top 5
    mock_reranked = [
        {"id": f"hit-{i}", "text": f"evidence text {i}", "score": 0.9 - (i * 0.1), "metadata": {"file_name": "doc.pdf", "page_number": i}, "milvus_score": 0.5}
        for i in range(1, 6)
    ]
    
    with patch("app.services.evaluation.rerank_hits", return_value=mock_reranked):
        result = await _search_one_requirement(req, mock_vs, "claims_col", "filter_expr")
        
        # Verify Milvus was queried for 10 candidates
        mock_vs.search.assert_called_once_with("claims_col", "Encryption at rest requirement", 10, "filter_expr")
        
        # Verify top-1 mapping
        assert result["req_id"] == "req-1"
        assert result["best_claim_id"] == "hit-1"
        assert result["similarity_score"] == 0.8  # First element's score
        assert result["is_covered"] is True
        
        # Verify evidence text combined top 5
        for i in range(1, 6):
            assert f"evidence text {i}" in result["evidence_text"]
            assert f"Chunk {i}" in result["evidence_text"]
            
        # Verify top_hits has 5 elements
        assert len(result["top_hits"]) == 5
        assert result["top_hits"][0]["score"] == 0.8
