

"""
Integration tests for agent_chat_loop with tool calling.

Tests:
1. Single-vendor question should make 1 search call
2. Comparative question should make 2+ search calls
3. Event types (token, citations, done) should be yielded correctly
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.llm import agent_chat_loop


@pytest.mark.asyncio
async def test_agent_single_vendor_search():
    """
    Single-vendor question should trigger exactly 1 tool call.
    Question: "What is Vendor A's SLA?"
    Expected: search_database called once with query about SLA, vendor_name="Vendor A"
    """

    # Mock search_database to track calls
    search_calls = []

    async def mock_search_database(query: str, vendor_name: str | None = None) -> list[dict]:
        search_calls.append({"query": query, "vendor_name": vendor_name})
        return [
            {
                "id": "hit-1",
                "text": "SLA commitment: 99.9% uptime",
                "metadata": {"vendor_name": vendor_name, "file_name": "proposal.pdf", "page_number": 1},
                "score": 0.95,
                "collection": "vendor_chunks",
            }
        ]

    # Mock Groq response with one tool call
    mock_groq = AsyncMock()
    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(
            message=MagicMock(
                content="Based on the search results, Vendor A's SLA is 99.9% uptime.",
                tool_calls=[
                    MagicMock(
                        id="call_001",
                        type="function",
                        function=SimpleNamespace(
                            name="search_database",
                            arguments=json.dumps({"query": "SLA uptime commitment", "vendor_name": "Vendor A"}),
                        ),
                    )
                ],
            ),
        )
    ]
    mock_response.choices[0].message.content = None  # First response has no content, only tool call

    # Mock streaming response for final answer
    stream_responses = [
        MagicMock(choices=[MagicMock(delta=MagicMock(content="Based "))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="on "))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="the "))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="search"))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="."))]),
    ]

    mock_final_response = MagicMock()
    mock_final_response.choices = [
        MagicMock(message=MagicMock(content="Final answer.", tool_calls=[]))
    ]
    mock_groq.chat = AsyncMock(side_effect=[mock_response, mock_final_response])

    # Make stream_chat an async generator
    async def mock_stream():
        for response in stream_responses:
            yield response.choices[0].delta.content

    mock_groq.stream_chat = MagicMock(return_value=mock_stream())

    with patch("app.services.llm.get_groq_client", return_value=mock_groq):
        events = []
        async for event in agent_chat_loop(
            question="What is Vendor A's SLA?",
            history=None,
            search_database=mock_search_database,
            max_tool_calls=3,
        ):
            events.append(event)

    # Verify exactly 1 search call
    assert len(search_calls) == 1
    assert search_calls[0]["query"] == "SLA uptime commitment"
    assert search_calls[0]["vendor_name"] == "Vendor A"

    # Verify event sequence
    token_events = [e for e in events if e["type"] == "token"]
    citation_events = [e for e in events if e["type"] == "citations"]
    done_events = [e for e in events if e["type"] == "done"]

    assert len(token_events) > 0, "Should have token events"
    assert len(citation_events) == 1, "Should have exactly 1 citations event"
    assert len(done_events) == 1, "Should have exactly 1 done event"


@pytest.mark.asyncio
async def test_agent_comparative_search():
    """
    Comparative question should trigger 2+ tool calls.
    Question: "Compare Vendor A and Vendor B's security features"
    Expected: search_database called at least twice (once per vendor)
    """

    search_calls = []

    async def mock_search_database(query: str, vendor_name: str | None = None) -> list[dict]:
        search_calls.append({"query": query, "vendor_name": vendor_name})

        if vendor_name and "A" in vendor_name:
            return [
                {
                    "id": "hit-a1",
                    "text": "Vendor A: ISO 27001 certified, AES-256 encryption",
                    "metadata": {"vendor_name": "Vendor A", "file_name": "proposal_a.pdf", "page_number": 2},
                    "score": 0.93,
                    "collection": "vendor_chunks",
                }
            ]
        elif vendor_name and "B" in vendor_name:
            return [
                {
                    "id": "hit-b1",
                    "text": "Vendor B: SOC 2 Type II, end-to-end encryption",
                    "metadata": {"vendor_name": "Vendor B", "file_name": "proposal_b.pdf", "page_number": 3},
                    "score": 0.91,
                    "collection": "vendor_chunks",
                }
            ]
        else:
            return []

    mock_groq = AsyncMock()

    # First response: no content, 2 tool calls (one per vendor)
    mock_response_1 = MagicMock()
    mock_response_1.choices = [
        MagicMock(
            message=MagicMock(
                content=None,
                tool_calls=[
                    MagicMock(
                        id="call_001",
                        type="function",
                        function=SimpleNamespace(
                            name="search_database",
                            arguments=json.dumps({"query": "security certifications", "vendor_name": "Vendor A"}),
                        ),
                    ),
                    MagicMock(
                        id="call_002",
                        type="function",
                        function=SimpleNamespace(
                            name="search_database",
                            arguments=json.dumps({"query": "security certifications", "vendor_name": "Vendor B"}),
                        ),
                    ),
                ],
            ),
        )
    ]

    mock_final_response = MagicMock()
    mock_final_response.choices = [
        MagicMock(message=MagicMock(content="Final comparison.", tool_calls=[]))
    ]
    mock_groq.chat = AsyncMock(side_effect=[mock_response_1, mock_final_response])

    stream_responses = [
        MagicMock(choices=[MagicMock(delta=MagicMock(content="Vendor "))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="A "))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="vs "))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="B."))]),
    ]

    async def mock_stream():
        for response in stream_responses:
            yield response.choices[0].delta.content

    mock_groq.stream_chat = MagicMock(return_value=mock_stream())

    with patch("app.services.llm.get_groq_client", return_value=mock_groq):
        events = []
        async for event in agent_chat_loop(
            question="Compare Vendor A and Vendor B's security features",
            history=None,
            search_database=mock_search_database,
            max_tool_calls=3,
        ):
            events.append(event)

    # Verify at least 2 search calls (one per vendor)
    assert len(search_calls) >= 2, f"Expected at least 2 searches, got {len(search_calls)}"
    vendors_searched = {call.get("vendor_name") for call in search_calls}
    assert "Vendor A" in vendors_searched or "Vendor B" in vendors_searched, "Should search both vendors"

    # Verify event types
    token_events = [e for e in events if e["type"] == "token"]
    citation_events = [e for e in events if e["type"] == "citations"]
    done_events = [e for e in events if e["type"] == "done"]

    assert len(token_events) > 0, "Should have token events"
    assert len(citation_events) == 1, "Should have exactly 1 citations event"
    assert len(done_events) == 1, "Should have exactly 1 done event"


@pytest.mark.asyncio
async def test_agent_respects_max_tool_calls():
    """
    Agent should not exceed max_tool_calls limit.
    Test with max_tool_calls=1 and a question that could trigger multiple searches.
    """

    search_calls = []

    async def mock_search_database(query: str, vendor_name: str | None = None) -> list[dict]:
        search_calls.append({"query": query, "vendor_name": vendor_name})
        return [
            {
                "id": f"hit-{len(search_calls)}",
                "text": f"Result for {vendor_name or 'general'}: {query}",
                "metadata": {"vendor_name": vendor_name, "file_name": "doc.pdf", "page_number": 1},
                "score": 0.9,
                "collection": "vendor_chunks",
            }
        ]

    mock_groq = AsyncMock()

    # Response 1: trigger search
    mock_response_1 = MagicMock()
    mock_response_1.choices = [
        MagicMock(
            message=MagicMock(
                content=None,
                tool_calls=[
                    MagicMock(
                        id="call_001",
                        type="function",
                        function=SimpleNamespace(
                            name="search_database",
                            arguments=json.dumps({"query": "feature 1", "vendor_name": None}),
                        ),
                    )
                ],
            ),
        )
    ]

    # Response 2: would trigger another search, but we're at max_tool_calls=1
    mock_response_2 = MagicMock()
    mock_response_2.choices = [
        MagicMock(
            message=MagicMock(
                content="This is the answer without calling more tools.",
                tool_calls=[
                    MagicMock(
                        id="call_002",
                        type="function",
                        function=SimpleNamespace(
                            name="search_database",
                            arguments=json.dumps({"query": "feature 2", "vendor_name": None}),
                        ),
                    )
                ],
            ),
        )
    ]

    mock_groq.chat = AsyncMock(side_effect=[mock_response_1, mock_response_2])

    stream_responses = [
        MagicMock(choices=[MagicMock(delta=MagicMock(content="Answer."))]),
    ]

    async def mock_stream():
        for response in stream_responses:
            yield response.choices[0].delta.content

    mock_groq.stream_chat = MagicMock(return_value=mock_stream())

    with patch("app.services.llm.get_groq_client", return_value=mock_groq):
        events = []
        async for event in agent_chat_loop(
            question="test question",
            history=None,
            search_database=mock_search_database,
            max_tool_calls=1,  # Restrict to 1 tool call
        ):
            events.append(event)

    # Should have made exactly 1 search due to max_tool_calls limit
    assert len(search_calls) == 1, f"Expected 1 search with max_tool_calls=1, got {len(search_calls)}"



@pytest.mark.asyncio
async def test_agent_specialized_tools():
    """
    Test that the agent can successfully invoke specialized tools when callbacks are present.
    """
    pricing_calls = []
    compare_calls = []
    matrix_calls = []

    async def mock_search_database(query: str, vendor_name: str | None = None) -> list[dict]:
        return []

    async def mock_get_vendor_pricing(vendor_name: str) -> dict:
        pricing_calls.append(vendor_name)
        return {"price": "$100/user/month", "vendor_name": vendor_name}

    async def mock_compare_vendors(requirement_id: str, vendor_a: str, vendor_b: str) -> dict:
        compare_calls.append((requirement_id, vendor_a, vendor_b))
        return {"comparison": f"{vendor_a} is better than {vendor_b}"}

    async def mock_get_rfp_matrix_weights() -> dict:
        matrix_calls.append(True)
        return {"weights": {"security": 0.3, "pricing": 0.7}}

    mock_groq = AsyncMock()

    # Response: LLM chooses to invoke all three specialized tools in sequence
    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(
            message=MagicMock(
                content=None,
                tool_calls=[
                    MagicMock(
                        id="call_pricing",
                        type="function",
                        function=SimpleNamespace(
                            name="get_vendor_pricing",
                            arguments=json.dumps({"vendor_name": "Vendor C"}),
                        ),
                    ),
                    MagicMock(
                        id="call_compare",
                        type="function",
                        function=SimpleNamespace(
                            name="compare_vendors",
                            arguments=json.dumps({"requirement_id": "BG.SEC.001", "vendor_a": "Vendor C", "vendor_b": "Vendor D"}),
                        ),
                    ),
                    MagicMock(
                        id="call_matrix",
                        type="function",
                        function=SimpleNamespace(
                            name="get_rfp_matrix_weights",
                            arguments="{}",
                        ),
                    ),
                ],
            ),
        )
    ]

    mock_final_response = MagicMock()
    mock_final_response.choices = [
        MagicMock(message=MagicMock(content="Final specialized response.", tool_calls=[]))
    ]
    mock_groq.chat = AsyncMock(side_effect=[mock_response, mock_final_response])

    stream_responses = [
        MagicMock(choices=[MagicMock(delta=MagicMock(content="Specialized "))]),
        MagicMock(choices=[MagicMock(delta=MagicMock(content="answer."))]),
    ]

    async def mock_stream():
        for response in stream_responses:
            yield response.choices[0].delta.content

    mock_groq.stream_chat = MagicMock(return_value=mock_stream())

    with patch("app.services.llm.get_groq_client", return_value=mock_groq):
        events = []
        async for event in agent_chat_loop(
            question="Please evaluate Vendor C's pricing and compare it.",
            history=None,
            search_database=mock_search_database,
            get_vendor_pricing=mock_get_vendor_pricing,
            compare_vendors=mock_compare_vendors,
            get_rfp_matrix_weights=mock_get_rfp_matrix_weights,
            max_tool_calls=5,
        ):
            events.append(event)

    # Verify that the callbacks were executed with the correct arguments
    assert len(pricing_calls) == 1
    assert pricing_calls[0] == "Vendor C"

    assert len(compare_calls) == 1
    assert compare_calls[0] == ("BG.SEC.001", "Vendor C", "Vendor D")

    assert len(matrix_calls) == 1

    # Verify event types and message flow
    thought_events = [e for e in events if e["type"] == "thought"]
    token_events = [e for e in events if e["type"] == "token"]

    assert len(thought_events) == 3, f"Expected 3 thoughts (one per tool call), got {len(thought_events)}"
    assert thought_events[0]["content"] == "Retrieving pricing information for 'Vendor C'..."
    assert thought_events[1]["content"] == "Comparing 'Vendor C' and 'Vendor D' on 'BG.SEC.001'..."
    assert thought_events[2]["content"] == "Retrieving RFP evaluation matrix weights..."

    assert len(token_events) > 0
