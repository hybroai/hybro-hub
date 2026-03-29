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

        async def fast_poll(self, agent, result, **kwargs):
            return await original_poll(self, agent, result, poll_interval=0, max_attempts=3)

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

        async def fast_poll(self, agent, result, **kwargs):
            return await original_poll(self, agent, result, poll_interval=0, max_attempts=3)

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
        body = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "message/send")
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "message/send"
        assert body["params"]["message"] == SAMPLE_MESSAGE
        assert "id" in body


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

        response = next(e for e in terminal_events if e["type"] == "agent_response")
        assert response["data"]["content"] == "Hello world"
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
        """Without configuration, params only contains 'message'."""
        from hub.dispatcher import Dispatcher

        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "message/send")
        assert "configuration" not in result["params"]
        assert result["params"]["message"] is SAMPLE_MESSAGE
        assert result["method"] == "message/send"

    def test_with_configuration_included_in_params(self):
        """With configuration dict, params['configuration'] is present."""
        from hub.dispatcher import Dispatcher

        cfg = {"blocking": True}
        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "message/send", configuration=cfg)
        assert result["params"]["configuration"] == {"blocking": True}
        assert result["params"]["message"] is SAMPLE_MESSAGE

    def test_streaming_call_omits_configuration(self):
        """_dispatch_streaming calls _build_jsonrpc without configuration."""
        from hub.dispatcher import Dispatcher

        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "message/stream")
        assert "configuration" not in result["params"]
        assert result["method"] == "message/stream"

    @pytest.mark.asyncio
    async def test_dispatch_sync_sends_blocking_true(self, agent):
        """Non-streaming dispatch must include blocking=True in the JSON-RPC params."""
        from hub.dispatcher import Dispatcher

        sent_body = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "message",
                "parts": [{"text": "blocking response"}],
            },
        }

        async def fake_post(url, *, json, headers):
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
        """Streaming dispatch must NOT include blocking in the JSON-RPC params."""
        import hub.dispatcher as dispatcher_mod
        from hub.dispatcher import Dispatcher

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
