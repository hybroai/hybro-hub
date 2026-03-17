"""Tests for hub.publish_queue."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from hub.config import PublishQueueConfig
from hub.publish_queue import (
    CRITICAL_EVENTS,
    PublishQueue,
    _coerce_agent_message_id,
)


@pytest.fixture
def qcfg():
    return PublishQueueConfig(
        max_size_mb=10,
        ttl_hours=1,
        drain_interval=30,
        drain_batch_size=20,
        max_retries_critical=5,
        max_retries_normal=3,
    )


@pytest.fixture
def queue(tmp_path, qcfg):
    q = PublishQueue(tmp_path / "test_queue.db", qcfg)
    q.open()
    yield q
    q.close()


# ──── coerce_agent_message_id ────

class TestCoerceAgentMessageId:
    def test_returns_existing_id(self):
        event = {"type": "agent_response", "agent_message_id": "abc123"}
        assert _coerce_agent_message_id(event) == "abc123"

    def test_falls_back_to_id_field(self):
        event = {"type": "agent_response", "id": "id-fallback"}
        assert _coerce_agent_message_id(event) == "id-fallback"

    def test_generates_uuid_when_missing(self):
        event = {"type": "agent_response"}
        mid = _coerce_agent_message_id(event)
        assert len(mid) == 32  # uuid4().hex


# ──── PublishQueue basic operations ────

class TestPublishQueueEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_returns_row_id(self, queue):
        rid = await queue.enqueue("room-1", "msg-1", {"type": "agent_response"})
        assert rid == 1

    @pytest.mark.asyncio
    async def test_enqueue_second_row_increments_id(self, queue):
        await queue.enqueue("room-1", "msg-1", {"type": "agent_response"})
        rid2 = await queue.enqueue("room-1", "msg-2", {"type": "task_submitted"})
        assert rid2 == 2

    @pytest.mark.asyncio
    async def test_critical_events_get_critical_max_retries(self, queue, qcfg):
        for et in CRITICAL_EVENTS:
            await queue.enqueue("room", "msg", {"type": et})
        events = await queue.get_ready_events(time.time() + 1)
        for _, _, _, _, _, max_retries in events:
            assert max_retries == qcfg.max_retries_critical

    @pytest.mark.asyncio
    async def test_artifact_update_gets_normal_max_retries(self, queue, qcfg):
        await queue.enqueue("room", "msg", {"type": "artifact_update"})
        events = await queue.get_ready_events(time.time() + 1)
        assert events[0][5] == qcfg.max_retries_normal

    @pytest.mark.asyncio
    async def test_normal_events_get_normal_max_retries(self, queue, qcfg):
        await queue.enqueue("room", "msg", {"type": "task_submitted"})
        events = await queue.get_ready_events(time.time() + 1)
        assert events[0][5] == qcfg.max_retries_normal


class TestPublishQueueGetReadyEvents:
    @pytest.mark.asyncio
    async def test_returns_events_past_next_retry_at(self, queue):
        await queue.enqueue("room", "msg", {"type": "agent_response"})
        events = await queue.get_ready_events(time.time() + 1)
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_does_not_return_future_events(self, queue):
        await queue.enqueue("room", "msg", {"type": "agent_response"})
        # Update retry to far future
        events = await queue.get_ready_events(time.time() + 1)
        event_id = events[0][0]
        await queue.update_retry(event_id, 1, time.time() + 9999)
        events = await queue.get_ready_events(time.time() + 1)
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_critical_events_returned_before_normal(self, queue):
        await queue.enqueue("room", "msg-normal", {"type": "task_submitted"}, priority=0)
        await queue.enqueue("room", "msg-critical", {"type": "agent_response"}, priority=1)
        events = await queue.get_ready_events(time.time() + 1)
        # First event should be the critical one (priority=1)
        assert events[0][2] == "msg-critical"

    @pytest.mark.asyncio
    async def test_respects_limit(self, queue):
        for i in range(5):
            await queue.enqueue("room", f"msg-{i}", {"type": "task_submitted"})
        events = await queue.get_ready_events(time.time() + 1, limit=3)
        assert len(events) == 3


class TestPublishQueueDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_event(self, queue):
        rid = await queue.enqueue("room", "msg", {"type": "agent_response"})
        await queue.delete(rid)
        events = await queue.get_ready_events(time.time() + 1)
        assert len(events) == 0


class TestPublishQueueUpdateRetry:
    @pytest.mark.asyncio
    async def test_update_sets_retry_count_and_next_retry(self, queue):
        rid = await queue.enqueue("room", "msg", {"type": "agent_response"})
        future = time.time() + 60
        await queue.update_retry(rid, 1, future, "transient")
        events = await queue.get_ready_events(future + 1)
        assert events[0][4] == 1  # retry_count


class TestPublishQueueGetStats:
    @pytest.mark.asyncio
    async def test_stats_empty(self, queue):
        stats = await queue.get_stats()
        assert stats == {"total": 0, "critical": 0, "normal": 0}

    @pytest.mark.asyncio
    async def test_stats_counts(self, queue):
        await queue.enqueue("room", "m1", {"type": "agent_response"}, priority=1)
        await queue.enqueue("room", "m2", {"type": "task_submitted"}, priority=0)
        stats = await queue.get_stats()
        assert stats["total"] == 2
        assert stats["critical"] == 1
        assert stats["normal"] == 1


# ──── Cleanup ────

class TestPublishQueueCleanupExpired:
    @pytest.mark.asyncio
    async def test_removes_old_events(self, tmp_path, qcfg):
        cfg = PublishQueueConfig(ttl_hours=0)  # everything expired immediately
        q = PublishQueue(tmp_path / "exp.db", cfg)
        q.open()
        try:
            await q.enqueue("room", "msg", {"type": "agent_response"})
            # Sleep a tiny bit to ensure created_at < cutoff
            await asyncio.sleep(0.01)
            removed = await q.cleanup_expired()
            assert removed == 1
            stats = await q.get_stats()
            assert stats["total"] == 0
        finally:
            q.close()

    @pytest.mark.asyncio
    async def test_keeps_recent_events(self, queue):
        await queue.enqueue("room", "msg", {"type": "agent_response"})
        removed = await queue.cleanup_expired()
        assert removed == 0
        stats = await queue.get_stats()
        assert stats["total"] == 1


class TestPublishQueueCleanupBySize:
    @pytest.mark.asyncio
    async def test_no_cleanup_when_under_limit(self, queue):
        await queue.enqueue("room", "msg", {"type": "agent_response"})
        removed = await queue.cleanup_by_size()
        assert removed == 0

    @pytest.mark.asyncio
    async def test_removes_events_when_over_limit(self, tmp_path):
        cfg = PublishQueueConfig(max_size_mb=0)  # always over limit
        q = PublishQueue(tmp_path / "size.db", cfg)
        q.open()
        try:
            for i in range(10):
                await q.enqueue("room", f"msg-{i}", {"type": "task_submitted"})
            removed = await q.cleanup_by_size()
            assert removed >= 1
        finally:
            q.close()


# ──── Config ────

class TestPublishQueueConfig:
    def test_defaults(self):
        cfg = PublishQueueConfig()
        assert cfg.enabled is True
        assert cfg.max_size_mb == 50
        assert cfg.ttl_hours == 24
        assert cfg.drain_interval == 30
        assert cfg.drain_batch_size == 20
        assert cfg.max_retries_critical == 20
        assert cfg.max_retries_normal == 5
        assert cfg.max_retries_streaming == 3
