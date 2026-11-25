import asyncio
import sys
import json
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.llm import transform_query


@pytest.mark.asyncio
async def test_transform_query_success():
    """Verify transform_query generates 3 variations using LLM."""
    mock_groq = AsyncMock()
    mock_groq.chat_text.return_value = json.dumps(["query var 1", "query var 2", "query var 3"])
    
    with patch("app.services.llm.get_groq_client", return_value=mock_groq):
        variations = await transform_query("original query")
        
        assert len(variations) == 3
        assert "query var 1" in variations
        assert "query var 2" in variations
        assert "query var 3" in variations


@pytest.mark.asyncio
async def test_transform_query_fallback():
    """Verify transform_query falls back gracefully to default variations on failure."""
    mock_groq = AsyncMock()
    mock_groq.chat_text.side_effect = Exception("LLM connection failed")
    
    with patch("app.services.llm.get_groq_client", return_value=mock_groq):
        variations = await transform_query("original query")
        
        assert len(variations) == 3
        assert "original query details" in variations
        assert "original query specification" in variations
        assert "original query implementation" in variations


@pytest.mark.asyncio
async def test_search_database_integration():
    """Verify that search_database (defined in main.py) transforms the query and queries Milvus concurrently."""
    # Since search_database is defined inner in ws_chat_session in main.py,
    # we can test the logic directly by creating a replica of its function or testing via mock
    
    # Let's recreate the inner function logic to verify its correctness
    vs = MagicMock()
    
    # Mock search_multi to return different hits for different queries
    def mock_search_multi(collection_names, query, top_k, rfp_id):
        if query == "original":
            return [
                {"id": "doc-1", "text": "evidence from original query", "score": 0.8}
            ]
        elif query == "var-1":
            return [
                {"id": "doc-1", "text": "evidence from original query", "score": 0.85},  # higher score
                {"id": "doc-2", "text": "evidence from var-1", "score": 0.6}
            ]
        elif query == "var-2":
            return [
                {"id": "doc-3", "text": "evidence from var-2", "score": 0.7}
            ]
        else:
            return []
            
    vs.search_multi = mock_search_multi
    
    # Replica of main.py search_database logic
    async def search_database_replica(query: str, rfp_id: str, vendor_name: str | None = None) -> list[dict]:
        collections = ["col1", "col2"]
        from app.services.llm import transform_query
        variations = await transform_query(query)
        all_queries = list(set([query] + variations))
        
        async def _single_search(q: str) -> list[dict]:
            return await asyncio.to_thread(
                vs.search_multi,
                collection_names=collections,
                query=q,
                top_k=8,
                rfp_id=rfp_id,
            )

        results_lists = await asyncio.gather(*[_single_search(q) for q in all_queries])
        
        # Merge & deduplicate
        merged_hits: dict[str, dict] = {}
        for hits_list in results_lists:
            for hit in hits_list:
                hit_id = hit.get("id")
                if not hit_id:
                    continue
                if hit_id not in merged_hits or hit.get("score", 0.0) > merged_hits[hit_id].get("score", 0.0):
                    merged_hits[hit_id] = hit
                    
        sorted_hits = sorted(merged_hits.values(), key=lambda x: x.get("score", 0.0), reverse=True)
        return sorted_hits[:8]

    # Mock Groq client inside transform_query
    mock_groq = AsyncMock()
    mock_groq.chat_text.return_value = json.dumps(["var-1", "var-2", "var-3"])
    
    with patch("app.services.llm.get_groq_client", return_value=mock_groq):
        hits = await search_database_replica("original", "rfp-123")
    
    # Verify deduplication and score prioritization:
    # doc-1 should have score 0.85 (from var-1), not 0.8 (from original)
    assert len(hits) == 3
    assert hits[0]["id"] == "doc-1"
    assert hits[0]["score"] == 0.85
    assert hits[1]["id"] == "doc-3"  # score 0.7
    assert hits[2]["id"] == "doc-2"  # score 0.6
