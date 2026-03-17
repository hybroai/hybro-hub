"""Disk-backed publish queue using SQLite.

Provides at-least-once delivery for relay events across hub crashes and
network outages. Events are written to disk only when immediate delivery
fails; the happy path (network healthy) has zero disk I/O overhead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import PublishQueueConfig

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id         TEXT    NOT NULL,
    agent_message_id TEXT   NOT NULL,
    event_json      TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL,
    next_retry_at   REAL    NOT NULL,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_next_retry
    ON publish_queue(next_retry_at);
CREATE INDEX IF NOT EXISTS idx_priority
    ON publish_queue(priority DESC, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_agent_message
    ON publish_queue(agent_message_id, event_type);
"""

CRITICAL_EVENTS = frozenset({"agent_response", "agent_error", "processing_status"})

# Row tuple returned by get_ready_events:
#   (id, room_id, agent_message_id, event_json, retry_count, max_retries)
_QueueRow = tuple[int, str, str, str, int, int]


def _coerce_agent_message_id(event: dict[str, Any]) -> str:
    """Return a non-empty agent_message_id for correlation, generating one if absent."""
    mid = event.get("agent_message_id") or event.get("id")
    if not mid:
        mid = uuid.uuid4().hex
        logger.warning(
            "Event of type %r missing agent_message_id — assigned %s",
            event.get("type", "unknown"),
            mid,
        )
    return mid


class PublishQueue:
    """Thread-safe SQLite-backed queue for relay events.

    All public methods are async and safe to call from the asyncio event loop.
    Synchronous DB work is dispatched via asyncio.to_thread to avoid blocking.
    A single asyncio.Lock serialises all DB access, which is sufficient given
    the low write rate expected from a single-user daemon.
    """

    def __init__(self, db_path: Path, config: PublishQueueConfig) -> None:
        self._db_path = db_path
        self._config = config
        self._max_size_bytes = config.max_size_mb * 1024 * 1024
        self._ttl_seconds = config.ttl_hours * 3600
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ──── Lifecycle ────

    def open(self) -> None:
        """Open (or create) the SQLite database. Call from an asyncio thread."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.debug("Publish queue opened at %s", self._db_path)

    def close(self) -> None:
        """Flush WAL and close the database. Call from an asyncio thread."""
        if self._conn:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                logger.debug("WAL checkpoint failed on close", exc_info=True)
            self._conn.close()
            self._conn = None

    # ──── Write operations ────

    async def enqueue(
        self,
        room_id: str,
        agent_message_id: str,
        event: dict[str, Any],
        *,
        priority: int = 0,
    ) -> int:
        """Persist an event to disk. Returns the new row id."""
        async with self._lock:
            return await asyncio.to_thread(
                self._enqueue_sync, room_id, agent_message_id, event, priority
            )

    def _enqueue_sync(
        self,
        room_id: str,
        agent_message_id: str,
        event: dict[str, Any],
        priority: int,
    ) -> int:
        now = time.time()
        event_type = event.get("type", "unknown")
        event_json = json.dumps(event)
        max_retries = self._max_retries_for(event_type)

        cur = self._conn.execute(  # type: ignore[union-attr]
            """
            INSERT INTO publish_queue
                (room_id, agent_message_id, event_json, event_type, priority,
                 created_at, max_retries, next_retry_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (room_id, agent_message_id, event_json, event_type, priority,
             now, max_retries, now),
        )
        self._conn.commit()  # type: ignore[union-attr]
        return cur.lastrowid  # type: ignore[return-value]

    async def update_retry(
        self,
        event_id: int,
        retry_count: int,
        next_retry_at: float,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_retry_sync, event_id, retry_count, next_retry_at, error
            )

    def _update_retry_sync(
        self,
        event_id: int,
        retry_count: int,
        next_retry_at: float,
        error: str | None,
    ) -> None:
        self._conn.execute(  # type: ignore[union-attr]
            """
            UPDATE publish_queue
               SET retry_count = ?, next_retry_at = ?, last_error = ?
             WHERE id = ?
            """,
            (retry_count, next_retry_at, error, event_id),
        )
        self._conn.commit()  # type: ignore[union-attr]

    async def delete(self, event_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, event_id)

    def _delete_sync(self, event_id: int) -> None:
        self._conn.execute(  # type: ignore[union-attr]
            "DELETE FROM publish_queue WHERE id = ?", (event_id,)
        )
        self._conn.commit()  # type: ignore[union-attr]

    # ──── Read operations ────

    async def get_ready_events(
        self, now: float, *, limit: int = 20
    ) -> list[_QueueRow]:
        """Return up to *limit* events whose next_retry_at <= now, priority-first."""
        async with self._lock:
            return await asyncio.to_thread(self._get_ready_events_sync, now, limit)

    def _get_ready_events_sync(self, now: float, limit: int) -> list[_QueueRow]:
        cur = self._conn.execute(  # type: ignore[union-attr]
            """
            SELECT id, room_id, agent_message_id, event_json,
                   retry_count, max_retries
              FROM publish_queue
             WHERE next_retry_at <= ?
             ORDER BY priority DESC, next_retry_at ASC
             LIMIT ?
            """,
            (now, limit),
        )
        return cur.fetchall()

    async def get_stats(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._get_stats_sync)

    def _get_stats_sync(self) -> dict[str, int]:
        cur = self._conn.execute(  # type: ignore[union-attr]
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN priority = 1 THEN 1 ELSE 0 END) AS critical,
                SUM(CASE WHEN priority = 0 THEN 1 ELSE 0 END) AS normal
              FROM publish_queue
            """
        )
        row = cur.fetchone()
        return {"total": row[0] or 0, "critical": row[1] or 0, "normal": row[2] or 0}

    # ──── Maintenance ────

    async def cleanup_expired(self) -> int:
        """Delete events older than TTL. Returns count removed."""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_expired_sync)

    def _cleanup_expired_sync(self) -> int:
        cutoff = time.time() - self._ttl_seconds
        cur = self._conn.execute(  # type: ignore[union-attr]
            "DELETE FROM publish_queue WHERE created_at < ?", (cutoff,)
        )
        self._conn.commit()  # type: ignore[union-attr]
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # type: ignore[union-attr]
        return cur.rowcount

    async def cleanup_by_size(self) -> int:
        """Delete oldest low-priority events when DB exceeds max_size_mb. Returns count removed."""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_by_size_sync)

    def _cleanup_by_size_sync(self) -> int:
        try:
            db_size = self._db_path.stat().st_size
        except FileNotFoundError:
            return 0
        if db_size <= self._max_size_bytes:
            return 0

        total = self._conn.execute(  # type: ignore[union-attr]
            "SELECT COUNT(*) FROM publish_queue"
        ).fetchone()[0]
        to_delete = max(1, total // 10)

        cur = self._conn.execute(  # type: ignore[union-attr]
            """
            DELETE FROM publish_queue
             WHERE id IN (
                 SELECT id FROM publish_queue
                  ORDER BY priority ASC, created_at ASC
                  LIMIT ?
             )
            """,
            (to_delete,),
        )
        self._conn.commit()  # type: ignore[union-attr]

        # VACUUM is expensive; only run when we're removing a large chunk.
        if to_delete > total * 0.2:
            self._conn.execute("VACUUM")  # type: ignore[union-attr]

        return cur.rowcount

    # ──── Helpers ────

    def _max_retries_for(self, event_type: str) -> int:
        if event_type in CRITICAL_EVENTS:
            return self._config.max_retries_critical
        return self._config.max_retries_normal
