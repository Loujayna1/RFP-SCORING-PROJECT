import pytest
import asyncio
from app.services.vector_store import get_vector_store

@pytest.mark.asyncio
async def test_hybrid_parent_child():
    vs = get_vector_store()
    await vs.connect()

    collection_name = "vendor_chunks_test_hybrid"
    
    # Drop to ensure a clean slate
    vs.drop_collection(collection_name)
    
    # We create a large paragraph to force parent-child chunking.
    # parent size is 2000, child is 400.
    large_text = "A" * 100 + " " + "This is a very specialized term: OBSCURE_TERM_123. " * 10 + "B" * 2000
    
    items = [
        {"id": "doc1", "text": large_text, "metadata": {"file_name": "test.pdf", "page_number": 1}}
    ]
    
    vs.upsert_batch(collection_name, items)
    
    # Wait for flush
    await asyncio.sleep(2)
    
    # Now we search for the obscure term, which should be caught by BM25 sparse vector
    hits = vs.search(collection_name, "OBSCURE_TERM_123", top_k=2)
    
    assert len(hits) > 0, "Should find hits"
    
    # Verify that the text returned is the parent text (i.e. length > 400)
    best_hit = hits[0]
    text_length = len(best_hit["text"])
    
    assert text_length > 400, f"Expected parent text (length > 400), got text of length {text_length}"
    assert "OBSCURE_TERM_123" in best_hit["text"], "Expected to find the term in the retrieved parent text"
    assert best_hit["metadata"]["chunk_type"] == "child", "The hit metadata should indicate it's a child hit, even though the text is swapped"
    
    # Clean up
    vs.drop_collection(collection_name)
