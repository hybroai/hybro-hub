"""Tests for hub.dispatcher."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from hub.agent_registry import LocalAgent
from hub.dispatcher import Dispatcher


@pytest.fixture
def agent():
    return LocalAgent(
        local_agent_id="test_001",
        name="Test Agent",
        url="http://localhost:9001",
        agent_card={"capabilities": {"streaming": False}},
    )


@pytest.fixture
def streaming_agent():
    return LocalAgent(
        local_agent_id="test_002",
        name="Streaming Agent",
        url="http://localhost:9002",
        agent_card={"capabilities": {"streaming": True}},
    )


SAMPLE_MESSAGE = {
    "messageId": "msg-123",
    "role": "user",
    "parts": [{"text": "Hello agent"}],
}


class TestDispatchSync:
    @pytest.mark.asyncio
    async def test_dispatch_sync_success(self, agent):
        dispatcher = Dispatcher()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "status": {
                    "state": "completed",
                    "message": {
                        "role": "agent",
                        "parts": [{"text": "Hi there!"}],
                    },
                },
            },
        }
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-001",
            user_message_id="um-001",
        ):
            events.extend(batch)

        assert len(events) == 2
        assert events[0]["type"] == "agent_response"
        assert events[0]["data"]["content"] == "Hi there!"
        assert events[0]["data"]["agent_id"] == "test_001"
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_dispatch_sync_error(self, agent):
        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(side_effect=Exception("connection failed"))
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-001",
        ):
            events.extend(batch)

        assert len(events) == 2
        assert events[0]["type"] == "agent_error"
        assert "connection failed" in events[0]["data"]["error"]
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_dispatch_error_with_empty_str(self, agent):
        """Exceptions whose str() is empty must still emit agent_error, not agent_response."""
        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(side_effect=TimeoutError())
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-002",
            user_message_id="um-002",
        ):
            events.extend(batch)

        assert events[0]["type"] == "agent_error"
        assert events[0]["data"]["error"]  # must be non-empty
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_dispatch_sync_nonterminal_polls_to_completed(self, agent, monkeypatch):
        """When message/send returns a non-terminal state, polling drives to completion."""
        import hub.dispatcher as dispatcher_mod

        dispatcher = Dispatcher()

        sync_resp = MagicMock()
        sync_resp.status_code = 200
        sync_resp.is_success = True
        sync_resp.raise_for_status = MagicMock()
        sync_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "task",
                "id": "task-poll-1",
                "status": {"state": "submitted"},
            },
        }

        poll_resp = MagicMock()
        poll_resp.status_code = 200
        poll_resp.is_success = True
        poll_resp.raise_for_status = MagicMock()
        poll_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "2",
            "result": {
                "kind": "task",
                "id": "task-poll-1",
                "status": {
                    "state": "completed",
                    "message": {
                        "role": "agent",
                        "parts": [{"text": "Polled result"}],
                    },
                },
            },
        }

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(side_effect=[sync_resp, poll_resp])
        dispatcher._client = mock_client

        original_poll = dispatcher_mod.Dispatcher._poll_until_terminal

        async def fast_poll(self, agent, result, poll_interval=2.0, max_attempts=30, interface=None):
            return await original_poll(self, agent, result, poll_interval=0, max_attempts=3, interface=interface)

        monkeypatch.setattr(dispatcher_mod.Dispatcher, "_poll_until_terminal", fast_poll)

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-poll-001",
            user_message_id="um-poll-001",
        ):
            events.extend(batch)

        assert events[0]["type"] == "agent_response"
        assert events[0]["data"]["content"] == "Polled result"
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_dispatch_sync_polling_exhausted(self, agent, monkeypatch):
        """When polling never reaches a terminal state, emit agent_error."""
        import hub.dispatcher as dispatcher_mod

        dispatcher = Dispatcher()

        sync_resp = MagicMock()
        sync_resp.status_code = 200
        sync_resp.is_success = True
        sync_resp.raise_for_status = MagicMock()
        sync_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "task",
                "id": "task-poll-stuck",
                "status": {"state": "working"},
            },
        }

        still_working_resp = MagicMock()
        still_working_resp.status_code = 200
        still_working_resp.is_success = True
        still_working_resp.raise_for_status = MagicMock()
        still_working_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "2",
            "result": {
                "kind": "task",
                "id": "task-poll-stuck",
                "status": {"state": "working"},
            },
        }

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(
            side_effect=[sync_resp] + [still_working_resp] * 3,
        )
        dispatcher._client = mock_client

        original_poll = dispatcher_mod.Dispatcher._poll_until_terminal

        async def fast_poll(self, agent, result, poll_interval=2.0, max_attempts=30, interface=None):
            return await original_poll(self, agent, result, poll_interval=0, max_attempts=3, interface=interface)

        monkeypatch.setattr(dispatcher_mod.Dispatcher, "_poll_until_terminal", fast_poll)

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-poll-002",
            user_message_id="um-poll-002",
        ):
            events.extend(batch)

        assert events[0]["type"] == "agent_error"
        assert "PollingTimeout" in events[0]["data"]["error_type"]
        assert "task-poll-stuck" in events[0]["data"]["error"]
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "failed"


class TestJsonRpcBuild:
    def test_build_jsonrpc(self):
        body = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "send", "0.3")
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "message/send"
        assert body["params"]["message"]["parts"] == [{"kind": "text", "text": "Hello agent"}]
        assert "id" in body

    def test_build_jsonrpc_v10(self):
        body = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "send", "1.0")
        assert body["method"] == "SendMessage"
        assert body["params"]["message"]["parts"] == [{"text": "Hello agent"}]


class TestExtractResponseContent:
    """Tests for Dispatcher._extract_response_content (replaces old _extract_text_from_response)."""

    def test_extract_from_status_message(self):
        result = {
            "result": {
                "kind": "task",
                "status": {
                    "state": "completed",
                    "message": {
                        "parts": [{"text": "Response text"}],
                    },
                },
            },
        }
        dr = Dispatcher._extract_response_content(result)
        assert dr.text == "Response text"

    def test_extract_from_artifacts(self):
        result = {
            "result": {
                "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [
                    {"parts": [{"text": "Artifact text"}]},
                ],
            },
        }
        dr = Dispatcher._extract_response_content(result)
        assert dr.text == "Artifact text"

    def test_extract_from_message_kind(self):
        result = {
            "result": {
                "kind": "message",
                "parts": [{"text": "Direct parts"}],
            },
        }
        dr = Dispatcher._extract_response_content(result)
        assert dr.text == "Direct parts"

    def test_extract_with_root_wrapper(self):
        result = {
            "result": {
                "kind": "task",
                "status": {
                    "state": "completed",
                    "message": {
                        "parts": [{"root": {"text": "Wrapped"}}],
                    },
                },
            },
        }
        dr = Dispatcher._extract_response_content(result)
        assert dr.text == "Wrapped"

    def test_fallback_no_kind(self):
        """Pre-spec agents that don't set kind still extract text."""
        result = {
            "result": {
                "status": {
                    "message": {
                        "parts": [{"text": "Fallback text"}],
                    },
                },
            },
        }
        dr = Dispatcher._extract_response_content(result)
        assert dr.text == "Fallback text"

    def test_task_state_extracted(self):
        result = {
            "result": {
                "kind": "task",
                "id": "task-1",
                "status": {
                    "state": "failed",
                    "message": {"parts": [{"text": "oops"}]},
                },
            },
        }
        dr = Dispatcher._extract_response_content(result)
        assert dr.task_state == "failed"
        assert dr.task_id == "task-1"


class TestExtractArtifactText:
    """Tests for Dispatcher._extract_artifact_text (replaces artifact path of old _extract_chunk_text)."""

    def test_artifact_text(self):
        data = {"artifact": {"parts": [{"text": "chunk"}]}}
        assert Dispatcher._extract_artifact_text(data) == "chunk"

    def test_artifact_with_root_wrapper(self):
        data = {"artifact": {"parts": [{"root": {"text": "nested"}}]}}
        assert Dispatcher._extract_artifact_text(data) == "nested"

    def test_multi_part_concatenated(self):
        data = {"artifact": {"parts": [{"text": "hello "}, {"text": "world"}]}}
        assert Dispatcher._extract_artifact_text(data) == "hello world"

    def test_empty(self):
        assert Dispatcher._extract_artifact_text({}) == ""


class TestExtractStatusText:
    """Tests for Dispatcher._extract_status_text (replaces status path of old _extract_chunk_text)."""

    def test_status_text(self):
        data = {"status": {"message": {"parts": [{"text": "done"}]}}}
        assert Dispatcher._extract_status_text(data) == "done"

    def test_status_with_root_wrapper(self):
        data = {"status": {"message": {"parts": [{"root": {"text": "nested"}}]}}}
        assert Dispatcher._extract_status_text(data) == "nested"

    def test_multi_part_concatenated(self):
        data = {"status": {"message": {"parts": [{"text": "step1 "}, {"text": "step2"}]}}}
        assert Dispatcher._extract_status_text(data) == "step1 step2"

    def test_empty(self):
        assert Dispatcher._extract_status_text({}) == ""


class TestExtractMessageText:
    """Tests for Dispatcher._extract_message_text."""

    def test_single_part(self):
        data = {"parts": [{"text": "hello"}]}
        assert Dispatcher._extract_message_text(data) == "hello"

    def test_multi_part_concatenated(self):
        data = {"parts": [{"text": "foo "}, {"text": "bar"}]}
        assert Dispatcher._extract_message_text(data) == "foo bar"

    def test_with_root_wrapper(self):
        data = {"parts": [{"root": {"text": "wrapped"}}]}
        assert Dispatcher._extract_message_text(data) == "wrapped"

    def test_empty(self):
        assert Dispatcher._extract_message_text({}) == ""


class TestCollectParts:
    """Tests for Dispatcher._collect_parts — separates text from non-text."""

    def test_text_parts(self):
        text, non_text = Dispatcher._collect_parts([{"text": "hello"}, {"text": " world"}])
        assert text == "hello world"
        assert non_text == []

    def test_mixed_parts(self):
        parts = [{"text": "hello"}, {"kind": "file", "uri": "file://x"}]
        text, non_text = Dispatcher._collect_parts(parts)
        assert text == "hello"
        assert len(non_text) == 1

    def test_root_wrapper(self):
        text, _ = Dispatcher._collect_parts([{"root": {"text": "wrapped"}}])
        assert text == "wrapped"

    def test_empty(self):
        text, non_text = Dispatcher._collect_parts([])
        assert text == ""
        assert non_text == []


class TestInteractiveState:
    """_emit_terminal_events must emit both task_interactive and processing_status."""

    def test_input_required_emits_both_events(self):
        dispatcher = Dispatcher()
        from hub.dispatcher import DispatchResult
        result = DispatchResult(task_state="input-required", task_id="t-1", context_id="ctx-1")
        events: list[dict] = []
        dispatcher._emit_terminal_events(events, result, "am-001", "um-001")

        types = [e["type"] for e in events]
        assert "task_interactive" in types
        assert "processing_status" in types

        interactive = next(e for e in events if e["type"] == "task_interactive")
        assert interactive["data"]["state"] == "input-required"
        assert interactive["data"]["task_id"] == "t-1"

        status = next(e for e in events if e["type"] == "processing_status")
        assert status["data"]["status"] == "input_required"
        assert status["data"]["user_message_id"] == "um-001"

    def test_auth_required_emits_both_events(self):
        dispatcher = Dispatcher()
        from hub.dispatcher import DispatchResult
        result = DispatchResult(task_state="auth-required")
        events: list[dict] = []
        dispatcher._emit_terminal_events(events, result, "am-002", None)

        types = [e["type"] for e in events]
        assert "task_interactive" in types
        assert "processing_status" in types


class TestEmitTerminalEventsAgentId:
    """agent_id is included in agent_response data when provided."""

    def test_agent_id_present_in_response(self):
        dispatcher = Dispatcher()
        from hub.dispatcher import DispatchResult
        result = DispatchResult(task_state="completed", text="done")
        events: list[dict] = []
        dispatcher._emit_terminal_events(events, result, "am-003", "um-003", agent_id="my-agent-42")

        response = next(e for e in events if e["type"] == "agent_response")
        assert response["data"]["agent_id"] == "my-agent-42"
        assert response["data"]["content"] == "done"

    def test_agent_id_absent_when_not_provided(self):
        dispatcher = Dispatcher()
        from hub.dispatcher import DispatchResult
        result = DispatchResult(task_state="completed", text="done")
        events: list[dict] = []
        dispatcher._emit_terminal_events(events, result, "am-004", "um-004")

        response = next(e for e in events if e["type"] == "agent_response")
        assert "agent_id" not in response["data"]


class TestCancelTask:
    """Tests for Dispatcher.cancel_task()."""

    @pytest.mark.asyncio
    async def test_cancel_success(self, agent):
        dispatcher = Dispatcher()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"jsonrpc": "2.0", "id": "1", "result": {}}
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher._client = mock_client

        await dispatcher.cancel_task(agent, "task-xyz")

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        body = call_kwargs[1]["json"]
        assert body["method"] == "tasks/cancel"
        assert body["params"]["id"] == "task-xyz"

    @pytest.mark.asyncio
    async def test_cancel_propagates_exception(self, agent):
        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(side_effect=Exception("network error"))
        dispatcher._client = mock_client

        with pytest.raises(Exception, match="network error"):
            await dispatcher.cancel_task(agent, "task-xyz")

    @pytest.mark.asyncio
    async def test_cancel_fallback_on_version_error(self):
        from hub.a2a_compat import ResolvedInterface

        agent = LocalAgent(
            local_agent_id="test_cancel_fb",
            name="Cancel Fallback Agent",
            url="http://localhost:9001",
            agent_card={"capabilities": {"streaming": False}},
            interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001/v1"),
            fallback_interface=ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001/v03"),
        )

        v10_resp = MagicMock()
        v10_resp.is_success = True
        v10_resp.json.return_value = {
            "jsonrpc": "2.0", "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }
        v03_resp = MagicMock()
        v03_resp.is_success = True
        v03_resp.json.return_value = {"jsonrpc": "2.0", "id": "2", "result": {}}

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(side_effect=[v10_resp, v03_resp])
        dispatcher._client = mock_client

        await dispatcher.cancel_task(agent, "task-42")

        assert mock_client.post.call_count == 2
        first_call = mock_client.post.call_args_list[0]
        assert first_call[0][0] == "http://localhost:9001/v1"
        assert first_call[1]["json"]["method"] == "CancelTask"
        second_call = mock_client.post.call_args_list[1]
        assert second_call[0][0] == "http://localhost:9001/v03"
        assert second_call[1]["json"]["method"] == "tasks/cancel"

    @pytest.mark.asyncio
    async def test_cancel_check_response_raises_non_fallback_error(self, agent):
        dispatcher = Dispatcher()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.json.return_value = {
            "jsonrpc": "2.0", "id": "1",
            "error": {"code": -32600, "message": "Invalid Request"},
        }
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher._client = mock_client

        with pytest.raises(RuntimeError, match="Invalid Request"):
            await dispatcher.cancel_task(agent, "task-xyz")


# ──── Helpers for streaming tests ────


@dataclass
class FakeSSE:
    data: str


class FakeEventSource:
    def __init__(self, events: list[dict]):
        self._events = events

    async def aiter_sse(self):
        for ev in self._events:
            yield FakeSSE(data=json.dumps(ev))


@asynccontextmanager
async def _fake_aconnect_sse(client, method, url, **kwargs):
    events = client._canned_events
    yield FakeEventSource(events)


class TestDispatchStreaming:
    """Tests for streaming dispatch — kind='message' emits artifact_update."""

    @pytest.mark.asyncio
    async def test_message_kind_emits_artifact_update(self, streaming_agent):
        """A2A kind='message' events produce artifact_update DispatchEvents."""
        canned = [
            {"result": {"kind": "task", "id": "t-1", "contextId": "ctx-1"}},
            {"result": {"kind": "message", "parts": [{"text": "Hello "}]}},
            {"result": {"kind": "message", "parts": [{"text": "world"}]}},
            {"result": {"kind": "status-update", "status": {"state": "completed"}, "final": True}},
        ]

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client._canned_events = canned
        dispatcher._client = mock_client

        import hub.dispatcher as dispatcher_mod
        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = _fake_aconnect_sse
        try:
            batches = []
            async for batch in dispatcher.dispatch(
                agent=streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-stream-001",
                user_message_id="um-001",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        streaming_events = [ev for b in batches[:-1] for ev in b]
        terminal_events = batches[-1]

        artifact_updates = [e for e in streaming_events if e["type"] == "artifact_update"]
        assert len(artifact_updates) == 2

        first = artifact_updates[0]
        assert first["data"]["append"] is True
        assert first["data"]["last_chunk"] is False
        assert first["data"]["text"] == "Hello "
        assert first["data"]["artifact"]["artifactId"] == "am-stream-001-stream"
        assert first["data"]["artifact"]["parts"] == [{"kind": "text", "text": "Hello "}]

        second = artifact_updates[1]
        assert second["data"]["text"] == "world"
        assert second["data"]["artifact"]["parts"] == [{"kind": "text", "text": "world"}]

        # Stream already emitted visible text and terminal adds no richer text,
        # so dispatcher suppresses duplicate terminal agent_response.
        assert not any(e["type"] == "agent_response" for e in terminal_events)
        status = next(e for e in terminal_events if e["type"] == "processing_status")
        assert status["data"]["status"] == "completed"
        assert status["data"]["user_message_id"] == "um-001"

    def test_emit_terminal_keeps_agent_response_when_terminal_text_is_richer(self):
        """Suppression should not trigger when terminal text adds richer content."""
        dispatcher = Dispatcher()
        from hub.dispatcher import DispatchResult

        result = DispatchResult(
            task_state="completed",
            artifact_text="Hello",
            text="Hello world with extra details",
        )
        events: list[dict] = []
        dispatcher._emit_terminal_events(
            events,
            result,
            "am-stream-001b",
            "um-001b",
            agent_id="test_002",
            stream_emitted_content=True,
        )

        response = next(e for e in events if e["type"] == "agent_response")
        assert response["data"]["content"] == "Hello world with extra details"
        assert response["data"]["agent_id"] == "test_002"

    @pytest.mark.asyncio
    async def test_native_artifact_update_unchanged(self, streaming_agent):
        """A2A kind='artifact-update' events still produce artifact_update with raw data."""
        canned = [
            {"result": {"kind": "task", "id": "t-2", "contextId": "ctx-2"}},
            {"result": {
                "kind": "artifact-update",
                "artifact": {"artifactId": "art-1", "parts": [{"text": "native chunk"}]},
                "append": True,
                "lastChunk": False,
            }},
            {"result": {"kind": "status-update", "status": {"state": "completed"}, "final": True}},
        ]

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client._canned_events = canned
        dispatcher._client = mock_client

        import hub.dispatcher as dispatcher_mod
        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = _fake_aconnect_sse
        try:
            batches = []
            async for batch in dispatcher.dispatch(
                agent=streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-stream-002",
                user_message_id="um-002",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        streaming_events = [ev for b in batches[:-1] for ev in b]
        artifact_updates = [e for e in streaming_events if e["type"] == "artifact_update"]
        assert len(artifact_updates) == 1

        ev = artifact_updates[0]
        assert ev["data"]["append"] is True
        assert ev["data"]["last_chunk"] is False
        assert ev["data"]["text"] == "native chunk"
        assert ev["data"]["raw"]["artifact"]["artifactId"] == "art-1"

    @pytest.mark.asyncio
    async def test_empty_message_skipped(self, streaming_agent):
        """A2A kind='message' events with no text and no parts are not emitted."""
        canned = [
            {"result": {"kind": "task", "id": "t-3"}},
            {"result": {"kind": "message", "parts": []}},
            {"result": {"kind": "status-update", "status": {"state": "completed"}, "final": True}},
        ]

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client._canned_events = canned

        refetch_resp = MagicMock()
        refetch_resp.is_success = True
        refetch_resp.json.return_value = {
            "jsonrpc": "2.0", "id": "1",
            "result": {"status": {"state": "completed", "message": {"role": "agent", "parts": []}}},
        }
        mock_client.post.return_value = refetch_resp
        dispatcher._client = mock_client

        import hub.dispatcher as dispatcher_mod
        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = _fake_aconnect_sse
        try:
            batches = []
            async for batch in dispatcher.dispatch(
                agent=streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-stream-003",
                user_message_id="um-003",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        streaming_events = [ev for b in batches[:-1] for ev in b]
        artifact_updates = [e for e in streaming_events if e["type"] == "artifact_update"]
        assert len(artifact_updates) == 0


# ---------------------------------------------------------------------------
# Fix B — _build_jsonrpc and _dispatch_sync blocking=True
# ---------------------------------------------------------------------------


class TestBuildJsonRpc:
    def test_no_configuration_omits_params_key(self):
        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "send", "0.3")
        assert "configuration" not in result["params"]
        assert result["method"] == "message/send"

    def test_with_configuration_included_in_params(self):
        cfg = {"blocking": True}
        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "send", "0.3", configuration=cfg)
        assert result["params"]["configuration"] == {"blocking": True}

    def test_streaming_call_omits_configuration(self):
        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "stream", "0.3")
        assert "configuration" not in result["params"]
        assert result["method"] == "message/stream"

    @pytest.mark.asyncio
    async def test_dispatch_sync_sends_blocking_true(self, agent):
        sent_body = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "message",
                "parts": [{"text": "blocking response"}],
            },
        }

        async def fake_post(url, *, json, headers, **kwargs):
            sent_body.update(json)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-block-001",
            user_message_id="um-block-001",
        ):
            events.extend(batch)

        assert sent_body["params"]["configuration"]["blocking"] is True
        assert events[0]["type"] == "agent_response"

    @pytest.mark.asyncio
    async def test_dispatch_streaming_does_not_send_blocking(self, streaming_agent):
        import hub.dispatcher as dispatcher_mod

        sent_body = {}

        @asynccontextmanager
        async def fake_sse(client, method, url, *, json, headers):
            sent_body.update(json)

            class _FakeEventSource:
                async def aiter_sse(self):
                    sse = MagicMock()
                    sse.data = '{"result": {"kind": "status-update", "status": {"state": "completed"}, "final": true}}'
                    yield sse

            yield _FakeEventSource()

        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = fake_sse
        try:
            dispatcher = Dispatcher()
            mock_client = AsyncMock()
            mock_client.is_closed = False
            dispatcher._client = mock_client

            batches = []
            async for batch in dispatcher.dispatch(
                agent=streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-stream-block",
                user_message_id="um-stream-block",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        assert "configuration" not in sent_body.get("params", {})


# ---------------------------------------------------------------------------
# V1.0 dual-protocol tests
# ---------------------------------------------------------------------------

import httpx
from hub.a2a_compat import ResolvedInterface, A2AVersionFallbackError


V10_INTERFACE = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001/a2a")
V03_INTERFACE = ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001")


@pytest.fixture
def v10_agent():
    return LocalAgent(
        local_agent_id="test_v10",
        name="V1.0 Agent",
        url="http://localhost:9001",
        agent_card={"capabilities": {"streaming": False}},
        interface=V10_INTERFACE,
    )


class TestV10MethodRouting:
    @pytest.mark.asyncio
    async def test_v10_sync_uses_sendmessage(self, v10_agent):
        sent_body = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": "done"}]},
                    },
                },
            },
        }

        async def fake_post(url, *, json, headers, **kwargs):
            sent_body.update(json)
            sent_body["_url"] = url
            sent_body["_headers"] = headers
            return mock_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=v10_agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-v10-001",
            user_message_id="um-001",
        ):
            events.extend(batch)

        assert sent_body["method"] == "SendMessage"
        assert sent_body["_url"] == "http://localhost:9001/a2a"
        assert sent_body["_headers"]["A2A-Version"] == "1.0"
        assert sent_body["params"]["configuration"] == {"returnImmediately": False}
        assert events[0]["type"] == "agent_response"
        assert events[0]["data"]["content"] == "done"

    @pytest.mark.asyncio
    async def test_v10_sync_strips_top_level_message_kind(self, v10_agent):
        sent_body = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": "done"}]},
                    },
                },
            },
        }

        async def fake_post(url, *, json, headers, **kwargs):
            sent_body.update(json)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        message = dict(SAMPLE_MESSAGE)
        message["kind"] = "message"

        async for _ in dispatcher.dispatch(
            agent=v10_agent,
            message_dict=message,
            agent_message_id="am-v10-kind",
            user_message_id="um-kind",
        ):
            pass

        assert "kind" not in sent_body["params"]["message"]

    @pytest.mark.asyncio
    async def test_v10_sync_strips_v03_kind_from_parts(self, v10_agent):
        sent_body = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": "done"}]},
                    },
                },
            },
        }

        async def fake_post(url, *, json, headers, **kwargs):
            sent_body.update(json)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        message = {
            "messageId": "msg-123",
            "role": "user",
            "parts": [{"kind": "text", "text": "Hello agent"}],
        }

        async for _ in dispatcher.dispatch(
            agent=v10_agent,
            message_dict=message,
            agent_message_id="am-v10-part-kind",
            user_message_id="um-part-kind",
        ):
            pass

        assert sent_body["params"]["message"]["parts"] == [{"text": "Hello agent"}]

    @pytest.mark.asyncio
    async def test_v10_sync_encodes_role_enum(self, v10_agent):
        sent_body = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": "done"}]},
                    },
                },
            },
        }

        async def fake_post(url, *, json, headers, **kwargs):
            sent_body.update(json)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        async for _ in dispatcher.dispatch(
            agent=v10_agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-v10-role",
            user_message_id="um-role",
        ):
            pass

        assert sent_body["params"]["message"]["role"] == "ROLE_USER"

    @pytest.mark.asyncio
    async def test_v10_cancel_uses_canceltask(self, v10_agent):
        dispatcher = Dispatcher()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {"jsonrpc": "2.0", "id": "1", "result": {}}
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher._client = mock_client

        await dispatcher.cancel_task(v10_agent, "task-xyz")

        call_kwargs = mock_client.post.call_args
        body = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
        assert body["method"] == "CancelTask"


class TestFetchTaskErrorHandling:
    @pytest.mark.asyncio
    async def test_fetch_task_raises_on_jsonrpc_error(self, v10_agent):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32600, "message": "Invalid params"},
        }
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        with pytest.raises(RuntimeError, match="JSON-RPC error -32600"):
            await dispatcher._fetch_task(v10_agent, "task-xyz")

    @pytest.mark.asyncio
    async def test_fetch_task_raises_fallback_on_method_not_found(self, v10_agent):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        with pytest.raises(A2AVersionFallbackError):
            await dispatcher._fetch_task(v10_agent, "task-xyz")


class TestCheckResponse:
    def test_unparseable_2xx_raises(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.is_success = True
        resp.json.side_effect = ValueError("No JSON")
        with pytest.raises(RuntimeError, match="unparseable body"):
            Dispatcher._check_response(resp)

    def test_unparseable_4xx_raises_for_status(self):
        resp = MagicMock()
        resp.status_code = 502
        resp.is_success = False
        resp.json.side_effect = ValueError("No JSON")
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Gateway", request=MagicMock(), response=resp,
        )
        with pytest.raises(httpx.HTTPStatusError):
            Dispatcher._check_response(resp)

    def test_valid_json_rpc_success(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {"jsonrpc": "2.0", "id": "1", "result": {"id": "t1"}}
        raw = Dispatcher._check_response(resp)
        assert raw["result"]["id"] == "t1"


@pytest.fixture
def v10_agent_with_fallback():
    return LocalAgent(
        local_agent_id="test_v10_fb",
        name="V1.0 Agent (fallback)",
        url="http://localhost:9001",
        agent_card={"capabilities": {"streaming": False}},
        interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001/v1"),
        fallback_interface=ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001/v03"),
    )


class TestFallbackRetry:
    @pytest.mark.asyncio
    async def test_sync_fallback_on_method_not_found(self, v10_agent_with_fallback):
        agent = v10_agent_with_fallback
        call_count = 0

        v10_error_resp = MagicMock()
        v10_error_resp.status_code = 200
        v10_error_resp.is_success = True
        v10_error_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }

        v03_success_resp = MagicMock()
        v03_success_resp.status_code = 200
        v03_success_resp.is_success = True
        v03_success_resp.raise_for_status = MagicMock()
        v03_success_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "2",
            "result": {
                "kind": "task",
                "id": "t-1",
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"text": "fallback ok"}]},
                },
            },
        }

        urls_called = []

        async def fake_post(url, *, json, headers, **kwargs):
            urls_called.append(url)
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return v10_error_resp
            return v03_success_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-fb-001",
            user_message_id="um-001",
        ):
            events.extend(batch)

        assert urls_called[0] == "http://localhost:9001/v1"
        assert urls_called[1] == "http://localhost:9001/v03"
        assert events[0]["type"] == "agent_response"
        assert events[0]["data"]["content"] == "fallback ok"

    @pytest.mark.asyncio
    async def test_no_fallback_without_fallback_interface(self):
        agent = LocalAgent(
            local_agent_id="test_nofb",
            name="No Fallback Agent",
            url="http://localhost:9001",
            agent_card={"capabilities": {"streaming": False}},
            interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001/v1"),
            fallback_interface=None,
        )

        error_resp = MagicMock()
        error_resp.status_code = 200
        error_resp.is_success = True
        error_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=error_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-nofb",
            user_message_id="um-nofb",
        ):
            events.extend(batch)

        assert events[0]["type"] == "agent_error"
        assert events[1]["data"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_non_fallback_jsonrpc_error_propagates(self):
        agent = LocalAgent(
            local_agent_id="test_other_err",
            name="Other Error Agent",
            url="http://localhost:9001",
            agent_card={"capabilities": {"streaming": False}},
            interface=V10_INTERFACE,
            fallback_interface=V03_INTERFACE,
        )

        error_resp = MagicMock()
        error_resp.status_code = 200
        error_resp.is_success = True
        error_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32600, "message": "Invalid Request"},
        }

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=error_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-other",
            user_message_id="um-other",
        ):
            events.extend(batch)

        assert mock_client.post.call_count == 1
        assert events[0]["type"] == "agent_error"


# ---------------------------------------------------------------------------
# V1.0 Streaming tests (Task 13)
# ---------------------------------------------------------------------------


@pytest.fixture
def v10_streaming_agent():
    return LocalAgent(
        local_agent_id="test_v10_stream",
        name="V1.0 Streaming Agent",
        url="http://localhost:9002",
        agent_card={"capabilities": {"streaming": True}},
        interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9002/a2a"),
    )


class TestV10Streaming:
    @pytest.mark.asyncio
    async def test_v10_stream_status_and_artifact(self, v10_streaming_agent):
        canned = [
            {"result": {"task": {"id": "t-1", "status": {"state": "TASK_STATE_SUBMITTED"}}}},
            {"result": {"artifactUpdate": {
                "artifact": {"artifactId": "art-1", "parts": [{"text": "v1 chunk"}]},
                "append": True,
                "lastChunk": False,
            }}},
            {"result": {"statusUpdate": {
                "status": {"state": "TASK_STATE_COMPLETED"},
            }}},
        ]

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client._canned_events = canned
        dispatcher._client = mock_client

        import hub.dispatcher as dispatcher_mod
        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = _fake_aconnect_sse
        try:
            batches = []
            async for batch in dispatcher.dispatch(
                agent=v10_streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-v10-stream",
                user_message_id="um-v10-stream",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        streaming_events = [ev for b in batches[:-1] for ev in b]
        terminal = batches[-1]

        artifact_updates = [e for e in streaming_events if e["type"] == "artifact_update"]
        assert len(artifact_updates) == 1
        assert artifact_updates[0]["data"]["text"] == "v1 chunk"

        status_events = [e for e in streaming_events if e["type"] == "task_status"]
        assert len(status_events) == 1
        assert status_events[0]["data"]["state"] == "completed"
        assert status_events[0]["data"]["final"] is True

        # Stream already emitted artifact text; terminal duplicate agent_response is suppressed.
        assert not any(e["type"] == "agent_response" for e in terminal)
        done = next(e for e in terminal if e["type"] == "processing_status")
        assert done["data"]["status"] == "completed"
        assert done["data"]["user_message_id"] == "um-v10-stream"

    @pytest.mark.asyncio
    async def test_v10_stream_message_kind(self, v10_streaming_agent):
        canned = [
            {"result": {"task": {"id": "t-2", "status": {"state": "TASK_STATE_SUBMITTED"}}}},
            {"result": {"message": {"role": "ROLE_AGENT", "parts": [{"text": "hello from v1"}]}}},
            {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}},
        ]

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client._canned_events = canned
        dispatcher._client = mock_client

        import hub.dispatcher as dispatcher_mod
        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = _fake_aconnect_sse
        try:
            batches = []
            async for batch in dispatcher.dispatch(
                agent=v10_streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-v10-msg",
                user_message_id="um-v10-msg",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        streaming_events = [ev for b in batches[:-1] for ev in b]
        artifact_updates = [e for e in streaming_events if e["type"] == "artifact_update"]
        assert len(artifact_updates) == 1
        assert artifact_updates[0]["data"]["text"] == "hello from v1"

    @pytest.mark.asyncio
    async def test_v10_stream_fallback_on_first_event_error(self):
        agent = LocalAgent(
            local_agent_id="test_stream_fb",
            name="Stream Fallback Agent",
            url="http://localhost:9002",
            agent_card={"capabilities": {"streaming": True}},
            interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9002/v1"),
            fallback_interface=ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9002/v03"),
        )

        v10_error_events = [
            {"jsonrpc": "2.0", "id": "1", "error": {"code": -32601, "message": "Method not found"}},
        ]
        v03_success_events = [
            {"result": {"kind": "task", "id": "t-fb", "contextId": "ctx-fb"}},
            {"result": {"kind": "status-update", "status": {"state": "completed"}, "final": True}},
        ]

        import hub.dispatcher as dispatcher_mod

        call_count = 0

        @asynccontextmanager
        async def switching_sse(client, method, url, *, json, headers):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield FakeEventSource(v10_error_events)
            else:
                yield FakeEventSource(v03_success_events)

        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = switching_sse
        try:
            dispatcher = Dispatcher()
            mock_client = AsyncMock()
            mock_client.is_closed = False

            refetch_resp = MagicMock()
            refetch_resp.is_success = True
            refetch_resp.json.return_value = {
                "jsonrpc": "2.0", "id": "1",
                "result": {"status": {"state": "completed", "message": {"role": "agent", "parts": []}}},
            }
            mock_client.post.return_value = refetch_resp
            dispatcher._client = mock_client

            batches = []
            async for batch in dispatcher.dispatch(
                agent=agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-stream-fb",
                user_message_id="um-stream-fb",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        assert call_count == 2
        terminal = batches[-1]
        status = next(e for e in terminal if e["type"] == "processing_status")
        assert status["data"]["status"] != "failed"


class TestStreamingNonFallbackError:
    """Bug fix: non-fallback JSON-RPC error on first SSE event must surface as agent_error."""

    @pytest.mark.asyncio
    async def test_non_fallback_jsonrpc_error_surfaces_as_error(self, v10_streaming_agent):
        canned = [
            {"jsonrpc": "2.0", "id": "1", "error": {"code": -32600, "message": "Invalid Request"}},
        ]

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client._canned_events = canned
        dispatcher._client = mock_client

        import hub.dispatcher as dispatcher_mod
        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = _fake_aconnect_sse
        try:
            batches = []
            async for batch in dispatcher.dispatch(
                agent=v10_streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-stream-err",
                user_message_id="um-stream-err",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        terminal = batches[-1]
        assert any(e["type"] == "agent_error" for e in terminal)
        assert any(e["data"]["status"] == "failed" for e in terminal if e["type"] == "processing_status")


class TestFetchTaskLocalFallback:
    """Bug fix: _fetch_task retries GetTask on fallback interface instead of bubbling."""

    @pytest.mark.asyncio
    async def test_fetch_task_retries_on_fallback_interface(self, v10_agent_with_fallback):
        agent = v10_agent_with_fallback
        call_count = 0
        urls_called = []

        v10_error_resp = MagicMock()
        v10_error_resp.status_code = 200
        v10_error_resp.is_success = True
        v10_error_resp.json.return_value = {
            "jsonrpc": "2.0", "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }

        v03_success_resp = MagicMock()
        v03_success_resp.status_code = 200
        v03_success_resp.is_success = True
        v03_success_resp.json.return_value = {
            "jsonrpc": "2.0", "id": "2",
            "result": {
                "kind": "task", "id": "t-1",
                "status": {"state": "completed",
                           "message": {"role": "agent", "parts": [{"text": "ok"}]}},
            },
        }

        async def fake_post(url, *, json, headers, **kwargs):
            nonlocal call_count
            urls_called.append(url)
            call_count += 1
            if call_count == 1:
                return v10_error_resp
            return v03_success_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        result = await dispatcher._fetch_task(agent, "t-1")

        assert call_count == 2
        assert urls_called[0] == "http://localhost:9001/v1"
        assert urls_called[1] == "http://localhost:9001/v03"
        assert result.get("status", {}).get("state") == "completed"

    @pytest.mark.asyncio
    async def test_fetch_task_no_fallback_bubbles(self, v10_agent):
        """Without fallback_interface, A2AVersionFallbackError still raises."""
        v10_error_resp = MagicMock()
        v10_error_resp.status_code = 200
        v10_error_resp.is_success = True
        v10_error_resp.json.return_value = {
            "jsonrpc": "2.0", "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=v10_error_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        with pytest.raises(A2AVersionFallbackError):
            await dispatcher._fetch_task(v10_agent, "t-1")
