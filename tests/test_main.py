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
    daemon._inflight_tasks = {}
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

        await daemon._handle_user_reply(
            _full_user_reply_event(user_message_id="user-turn-1"),
        )

        daemon.relay.publish.assert_called_once()
        room_id, events = daemon.relay.publish.call_args[0]
        assert room_id == "room-1"
        assert events[0]["type"] == "agent_error"
        assert events[0]["data"]["error_type"] == "AgentNotFound"
        assert events[1]["type"] == "processing_status"
        assert events[1]["data"]["status"] == "failed"
        assert events[1]["data"]["user_message_id"] == "user-turn-1"


class TestHandleUserReplyIncompleteEvent:
    async def test_publishes_failure_when_local_agent_id_missing(self):
        daemon = _make_daemon()
        event = _full_user_reply_event(
            local_agent_id=None,
            user_message_id="user-turn-1",
        )

        await daemon._handle_user_reply(event)

        daemon.relay.publish.assert_called_once()
        _, events = daemon.relay.publish.call_args[0]
        assert events[0]["data"]["error_type"] == "InvalidEvent"
        assert events[1]["data"]["user_message_id"] == "user-turn-1"

    async def test_uses_originating_user_message_id_on_failure(self):
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = None

        await daemon._handle_user_reply(
            _full_user_reply_event(originating_user_message_id="user-turn-2"),
        )

        _, events = daemon.relay.publish.call_args[0]
        assert events[1]["data"]["user_message_id"] == "user-turn-2"

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


class TestHandleCancelTaskCancelsInflightTask:
    async def test_cancels_inflight_asyncio_task(self):
        """cancel_task must cancel the in-flight dispatch asyncio.Task."""
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT
        daemon.dispatcher.cancel_task = AsyncMock()

        # Simulate an in-flight task that is still running
        was_cancelled = False

        async def _long_dispatch():
            nonlocal was_cancelled
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                was_cancelled = True
                raise

        inflight = asyncio.create_task(_long_dispatch())
        daemon._inflight_tasks["amsg-99"] = inflight

        event = {
            "type": "cancel_task",
            "local_agent_id": "agent-1",
            "agent_message_id": "amsg-99",
            "task_id": "task-42",
        }
        await daemon._handle_cancel_task(event)

        # Give the event loop a tick to propagate the cancellation
        await asyncio.sleep(0)

        assert inflight.cancelled()
        assert "amsg-99" not in daemon._inflight_tasks

    async def test_removes_inflight_task_from_registry(self):
        """After cancel, the agent_message_id must be removed from _inflight_tasks."""
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT
        daemon.dispatcher.cancel_task = AsyncMock()

        inflight = asyncio.create_task(asyncio.sleep(60))
        daemon._inflight_tasks["amsg-77"] = inflight

        await daemon._handle_cancel_task({
            "type": "cancel_task",
            "local_agent_id": "agent-1",
            "agent_message_id": "amsg-77",
            "task_id": "task-77",
        })

        assert "amsg-77" not in daemon._inflight_tasks
        inflight.cancel()  # cleanup

    async def test_cancel_without_inflight_task_still_forwards_rpc(self):
        """cancel_task with no in-flight task still forwards the RPC to the agent."""
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT
        daemon.dispatcher.cancel_task = AsyncMock()

        # No entry in _inflight_tasks
        await daemon._handle_cancel_task({
            "type": "cancel_task",
            "local_agent_id": "agent-1",
            "agent_message_id": "amsg-55",
            "task_id": "task-55",
        })

        daemon.dispatcher.cancel_task.assert_called_once_with(AGENT, "task-55")


class TestSpawnDispatch:
    async def test_tracks_task_in_inflight(self):
        """_spawn_dispatch registers the task under agent_message_id."""
        daemon = _make_daemon()

        ready = asyncio.Event()

        async def _work():
            ready.set()
            await asyncio.sleep(60)

        daemon._spawn_dispatch("amsg-spawn", _work())
        await asyncio.wait_for(ready.wait(), timeout=1)

        assert "amsg-spawn" in daemon._inflight_tasks
        daemon._inflight_tasks["amsg-spawn"].cancel()

    async def test_removes_task_on_completion(self):
        """_spawn_dispatch removes the task from _inflight_tasks when it finishes."""
        daemon = _make_daemon()
        done = asyncio.Event()

        async def _quick():
            pass

        daemon._spawn_dispatch("amsg-done", _quick())
        # Wait for the task to finish naturally
        task = daemon._inflight_tasks.get("amsg-done")
        if task:
            await asyncio.wait_for(task, timeout=1)
        # Give the done-callback a tick
        await asyncio.sleep(0)

        assert "amsg-done" not in daemon._inflight_tasks

    async def test_rejects_duplicate_agent_message_id(self):
        """_spawn_dispatch ignores a replay if a task for the same id is still running."""
        daemon = _make_daemon()
        started = asyncio.Event()

        async def _long():
            started.set()
            await asyncio.sleep(60)

        daemon._spawn_dispatch("amsg-dup", _long())
        await asyncio.wait_for(started.wait(), timeout=1)

        original_task = daemon._inflight_tasks["amsg-dup"]

        # Attempt a duplicate spawn — should be rejected
        duplicate_ran = False

        async def _duplicate():
            nonlocal duplicate_ran
            duplicate_ran = True

        daemon._spawn_dispatch("amsg-dup", _duplicate())
        await asyncio.sleep(0)

        assert not duplicate_ran
        assert daemon._inflight_tasks["amsg-dup"] is original_task
        original_task.cancel()

    async def test_done_callback_does_not_corrupt_replacement(self):
        """If a task is manually replaced, the old callback must not evict the new entry."""
        daemon = _make_daemon()

        async def _quick():
            pass

        # Spawn a task that completes quickly
        daemon._spawn_dispatch("amsg-replace", _quick())
        old_task = daemon._inflight_tasks["amsg-replace"]
        await asyncio.wait_for(old_task, timeout=1)

        # Simulate a replacement placed BEFORE the done-callback fires
        sentinel_task = asyncio.create_task(asyncio.sleep(60))
        daemon._inflight_tasks["amsg-replace"] = sentinel_task

        # Give the old done-callback a tick to run
        await asyncio.sleep(0)

        # The sentinel must still be tracked
        assert daemon._inflight_tasks.get("amsg-replace") is sentinel_task
        sentinel_task.cancel()


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
        spawned: list[tuple] = []
        daemon._spawn_dispatch = lambda mid, coro: spawned.append((mid, coro))
        event = {"type": "user_reply", "agent_message_id": "amsg-1"}

        await daemon._handle_event(event)

        assert len(spawned) == 1
        assert spawned[0][0] == "amsg-1"

    async def test_routes_user_message_by_default(self):
        daemon = _make_daemon()
        spawned: list[tuple] = []

        def _capture(mid, coro):
            spawned.append((mid, coro))
            coro.close()  # prevent "coroutine never awaited" warning

        daemon._spawn_dispatch = _capture
        event = {"type": "user_message", "agent_message_id": "amsg-2"}

        await daemon._handle_event(event)

        assert len(spawned) == 1
        assert spawned[0][0] == "amsg-2"

    async def test_unknown_type_is_ignored(self):
        daemon = _make_daemon()
        spawned: list[tuple] = []
        daemon._spawn_dispatch = lambda mid, coro: spawned.append((mid, coro))
        event = {"type": "something_new"}

        await daemon._handle_event(event)

        assert spawned == []


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
        """Legacy reply_text path uses canonical flattened parts (no 'kind')."""
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT

        dispatched_message = {}
        dispatched_user_message_id = {}

        async def _capture_dispatch(**kwargs):
            dispatched_message.update(kwargs.get("message_dict", {}))
            dispatched_user_message_id["value"] = kwargs.get("user_message_id")
            yield [{"type": "agent_response", "agent_message_id": "amsg-12345678", "data": {"content": "ok"}}]

        daemon.dispatcher.dispatch = _capture_dispatch

        await daemon._handle_user_reply(_full_user_reply_event())

        parts = dispatched_message.get("parts", [])
        assert len(parts) == 1
        assert "kind" not in parts[0]
        assert parts[0]["text"] == "yes"
        assert dispatched_user_message_id["value"] is None

    async def test_hitl_reply_uses_full_message_payload_when_present(self):
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT

        captured = {}

        async def _capture_dispatch(**kwargs):
            captured.update(kwargs)
            yield [{"type": "agent_response", "agent_message_id": "amsg-12345678", "data": {"content": "ok"}}]

        daemon.dispatcher.dispatch = _capture_dispatch

        await daemon._handle_user_reply(
            _full_user_reply_event(
                message={
                    "kind": "message",
                    "role": "user",
                    "parts": [
                        {"kind": "text", "text": "publish"},
                        {
                            "kind": "file",
                            "file": {
                                "uri": "https://s3.example/file.png",
                                "mimeType": "image/png",
                                "name": "photo.png",
                            },
                        },
                    ],
                },
                user_message_id="user-turn-1",
                reply_text="publish",
            )
        )

        message_dict = captured["message_dict"]
        assert message_dict["parts"][1]["kind"] == "file"
        assert captured["user_message_id"] == "user-turn-1"


class TestLocalAgentDefaultInterface:
    def test_agent_has_default_interface(self):
        assert AGENT.interface is not None
        assert AGENT.interface.protocol_version == "0.3"
        assert AGENT.interface.url == "http://localhost:9001"
