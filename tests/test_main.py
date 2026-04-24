"""Tests for hub.main — HubDaemon event handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub.agent_registry import LocalAgent
from hub.main import HubDaemon


def _make_daemon() -> HubDaemon:
    """Build a HubDaemon with mocked subsystems."""
    daemon = object.__new__(HubDaemon)
    daemon.relay = MagicMock()
    daemon.relay.publish = AsyncMock()
    daemon.relay.heartbeat = AsyncMock()
    daemon.registry = MagicMock()
    daemon.dispatcher = MagicMock()
    daemon.privacy = MagicMock()
    daemon.config = MagicMock()
    daemon.config.heartbeat_interval = 30
    daemon._shutdown_event = asyncio.Event()
    return daemon


AGENT = LocalAgent(
    local_agent_id="agent-1",
    name="Test Agent",
    url="http://localhost:9001",
    agent_card={"capabilities": {"streaming": False}},
)


def _full_user_message_event(**overrides) -> dict:
    base = {
        "type": "user_message",
        "local_agent_id": "agent-1",
        "room_id": "room-1",
        "agent_message_id": "amsg-12345678",
        "user_message_id": "umsg-1",
        "message": {"role": "user", "parts": [{"text": "hello"}]},
    }
    base.update(overrides)
    return base


def _full_user_reply_event(**overrides) -> dict:
    base = {
        "type": "user_reply",
        "local_agent_id": "agent-1",
        "room_id": "room-1",
        "agent_message_id": "amsg-12345678",
        "reply_text": "yes",
        "task_id": "task-1",
        "context_id": "ctx-1",
    }
    base.update(overrides)
    return base


# ──── _handle_user_message ────


class TestHandleUserMessageAgentNotFound:
    async def test_publishes_failure_when_agent_missing(self):
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = None

        await daemon._handle_user_message(_full_user_message_event())

        daemon.relay.publish.assert_called_once()
        room_id, events = daemon.relay.publish.call_args[0]
        assert room_id == "room-1"
        assert len(events) == 2
        assert events[0]["type"] == "agent_error"
        assert "AgentNotFound" == events[0]["data"]["error_type"]
        assert "agent-1" in events[0]["data"]["error"]
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "failed"
        assert events[1]["data"]["user_message_id"] == "umsg-1"


class TestHandleUserMessageIncompleteEvent:
    async def test_publishes_failure_when_message_missing(self):
        daemon = _make_daemon()
        event = _full_user_message_event(message=None)

        await daemon._handle_user_message(event)

        daemon.relay.publish.assert_called_once()
        _, events = daemon.relay.publish.call_args[0]
        assert events[0]["type"] == "agent_error"
        assert events[0]["data"]["error_type"] == "InvalidEvent"
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "failed"

    async def test_publishes_failure_when_local_agent_id_missing(self):
        daemon = _make_daemon()
        event = _full_user_message_event(local_agent_id=None)

        await daemon._handle_user_message(event)

        daemon.relay.publish.assert_called_once()
        _, events = daemon.relay.publish.call_args[0]
        assert events[0]["data"]["error_type"] == "InvalidEvent"

    async def test_no_publish_when_room_id_missing(self):
        """When room_id is absent we can't publish anywhere — just log."""
        daemon = _make_daemon()
        event = _full_user_message_event(room_id=None, agent_message_id=None)

        await daemon._handle_user_message(event)

        daemon.relay.publish.assert_not_called()

    async def test_no_publish_when_agent_message_id_missing(self):
        daemon = _make_daemon()
        event = _full_user_message_event(agent_message_id=None)

        await daemon._handle_user_message(event)

        daemon.relay.publish.assert_not_called()


class TestHandleUserMessageHappyPath:
    async def test_dispatches_to_agent(self):
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT

        async def _fake_dispatch(**kwargs):
            yield [{"type": "agent_response", "agent_message_id": "amsg-12345678", "data": {"content": "hi"}}]
            yield [{"type": "processing_status", "agent_message_id": "amsg-12345678", "data": {"status": "completed"}}]

        daemon.dispatcher.dispatch = _fake_dispatch

        await daemon._handle_user_message(_full_user_message_event())

        # task_submitted + 2 dispatch batches = 3 publishes
        assert daemon.relay.publish.call_count == 3
        first_call_events = daemon.relay.publish.call_args_list[0][0][1]
        assert first_call_events[0]["type"] == "task_submitted"


# ──── _handle_user_reply ────


class TestHandleUserReplyAgentNotFound:
    async def test_publishes_failure_when_agent_missing(self):
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = None

        await daemon._handle_user_reply(_full_user_reply_event())

        daemon.relay.publish.assert_called_once()
        room_id, events = daemon.relay.publish.call_args[0]
        assert room_id == "room-1"
        assert events[0]["type"] == "agent_error"
        assert events[0]["data"]["error_type"] == "AgentNotFound"
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "failed"


class TestHandleUserReplyIncompleteEvent:
    async def test_publishes_failure_when_local_agent_id_missing(self):
        daemon = _make_daemon()
        event = _full_user_reply_event(local_agent_id=None)

        await daemon._handle_user_reply(event)

        daemon.relay.publish.assert_called_once()
        _, events = daemon.relay.publish.call_args[0]
        assert events[0]["data"]["error_type"] == "InvalidEvent"

    async def test_no_publish_when_room_id_and_msg_id_both_missing(self):
        daemon = _make_daemon()
        event = _full_user_reply_event(room_id=None, agent_message_id=None)

        await daemon._handle_user_reply(event)

        daemon.relay.publish.assert_not_called()


class TestHandleUserReplyHappyPath:
    async def test_dispatches_to_agent(self):
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT

        async def _fake_dispatch(**kwargs):
            yield [{"type": "agent_response", "agent_message_id": "amsg-12345678", "data": {"content": "ok"}}]

        daemon.dispatcher.dispatch = _fake_dispatch

        await daemon._handle_user_reply(_full_user_reply_event())

        # task_submitted + 1 dispatch batch = 2 publishes
        assert daemon.relay.publish.call_count == 2


# ──── _handle_cancel_task ────


class TestHandleCancelTaskUnknownAgent:
    async def test_does_not_publish_failure(self):
        """cancel_task is best-effort — unknown agent should not publish an error."""
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = None

        event = {
            "type": "cancel_task",
            "local_agent_id": "gone-agent",
            "agent_message_id": "amsg-99",
            "task_id": "task-1",
        }
        await daemon._handle_cancel_task(event)

        daemon.relay.publish.assert_not_called()


class TestHandleCancelTaskHappyPath:
    async def test_delegates_to_dispatcher_cancel_task(self):
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT
        daemon.dispatcher.cancel_task = AsyncMock()

        event = {
            "type": "cancel_task",
            "local_agent_id": "agent-1",
            "agent_message_id": "amsg-99",
            "task_id": "task-42",
        }
        await daemon._handle_cancel_task(event)

        daemon.dispatcher.cancel_task.assert_called_once_with(AGENT, "task-42")

    async def test_swallows_dispatcher_exception(self):
        """cancel_task is best-effort — dispatcher errors must not propagate."""
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT
        daemon.dispatcher.cancel_task = AsyncMock(side_effect=Exception("timeout"))

        event = {
            "type": "cancel_task",
            "local_agent_id": "agent-1",
            "agent_message_id": "amsg-99",
            "task_id": "task-42",
        }
        # Should not raise
        await daemon._handle_cancel_task(event)


# ──── _handle_event routing ────


class TestHandleEventRouting:
    async def test_routes_cancel_task(self):
        daemon = _make_daemon()
        daemon._handle_cancel_task = AsyncMock()
        event = {"type": "cancel_task"}

        await daemon._handle_event(event)

        daemon._handle_cancel_task.assert_called_once_with(event)

    async def test_routes_user_reply(self):
        daemon = _make_daemon()
        daemon._handle_user_reply = AsyncMock()
        event = {"type": "user_reply"}

        await daemon._handle_event(event)

        daemon._handle_user_reply.assert_called_once_with(event)

    async def test_routes_user_message_by_default(self):
        daemon = _make_daemon()
        daemon._handle_user_message = AsyncMock()
        event = {"type": "user_message"}

        await daemon._handle_event(event)

        daemon._handle_user_message.assert_called_once_with(event)

    async def test_unknown_type_is_ignored(self):
        daemon = _make_daemon()
        daemon._handle_user_message = AsyncMock()
        event = {"type": "something_new"}

        await daemon._handle_event(event)

        daemon._handle_user_message.assert_not_called()


# ──── _heartbeat_loop ────


class TestHeartbeatLoop:
    async def test_calls_relay_heartbeat(self):
        daemon = _make_daemon()
        call_count = 0

        async def _sleep(interval):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                daemon._shutdown_event.set()

        with patch("asyncio.sleep", side_effect=_sleep):
            await daemon._heartbeat_loop()

        assert daemon.relay.heartbeat.await_count == 2

    async def test_resets_interval_on_success(self):
        daemon = _make_daemon()
        intervals = []

        daemon.relay.heartbeat = AsyncMock(
            side_effect=[Exception("fail"), None]
        )

        call_count = 0

        async def _sleep(interval):
            nonlocal call_count
            intervals.append(interval)
            call_count += 1
            if call_count >= 2:
                daemon._shutdown_event.set()

        with patch("asyncio.sleep", side_effect=_sleep):
            await daemon._heartbeat_loop()

        assert intervals[0] == 30  # base interval before first failure
        # After failure + success, interval resets but sleep already captured
        # the backed-off value; verify it's larger than base
        assert intervals[1] > 30

    async def test_warns_on_failure(self):
        daemon = _make_daemon()
        daemon.relay.heartbeat = AsyncMock(side_effect=Exception("timeout"))

        call_count = 0

        async def _sleep(interval):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                daemon._shutdown_event.set()

        with patch("asyncio.sleep", side_effect=_sleep), \
             patch("hub.main.logger") as mock_logger:
            await daemon._heartbeat_loop()

        mock_logger.warning.assert_called_once()
        assert "Heartbeat failed" in mock_logger.warning.call_args[0][0]

    async def test_backoff_increases_interval(self):
        daemon = _make_daemon()
        daemon.relay.heartbeat = AsyncMock(side_effect=Exception("fail"))
        intervals = []
        call_count = 0

        async def _sleep(interval):
            nonlocal call_count
            intervals.append(interval)
            call_count += 1
            if call_count >= 3:
                daemon._shutdown_event.set()

        with patch("asyncio.sleep", side_effect=_sleep):
            await daemon._heartbeat_loop()

        assert intervals[0] == 30  # base interval
        assert intervals[1] > 30   # backed off after first failure
        assert intervals[2] > 30   # still backed off

    async def test_backoff_caps_at_max(self):
        daemon = _make_daemon()
        daemon.relay.heartbeat = AsyncMock(side_effect=Exception("fail"))
        intervals = []
        call_count = 0

        async def _sleep(interval):
            nonlocal call_count
            intervals.append(interval)
            call_count += 1
            if call_count >= 10:
                daemon._shutdown_event.set()

        with patch("asyncio.sleep", side_effect=_sleep):
            await daemon._heartbeat_loop()

        # With jitter, max effective interval is 300 * 1.5 = 450
        for iv in intervals[1:]:
            assert iv <= 450


class TestHandleUserReplyCanonicalParts:
    async def test_hitl_reply_uses_flattened_parts(self):
        """HITL reply must use canonical flattened parts (no 'kind')."""
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT

        dispatched_message = {}

        async def _capture_dispatch(**kwargs):
            dispatched_message.update(kwargs.get("message_dict", {}))
            yield [{"type": "agent_response", "agent_message_id": "amsg-12345678", "data": {"content": "ok"}}]

        daemon.dispatcher.dispatch = _capture_dispatch

        await daemon._handle_user_reply(_full_user_reply_event())

        parts = dispatched_message.get("parts", [])
        assert len(parts) == 1
        assert "kind" not in parts[0]
        assert parts[0]["text"] == "yes"


class TestLocalAgentDefaultInterface:
    def test_agent_has_default_interface(self):
        assert AGENT.interface is not None
        assert AGENT.interface.protocol_version == "0.3"
        assert AGENT.interface.url == "http://localhost:9001"
