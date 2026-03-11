# Disk-Backed Publish Queue Design

## Overview

This document describes a disk-backed queue for reliable event delivery from
hybro-hub to the cloud backend. The design ensures that critical events
(especially `agent_response` and `agent_error`) survive hub crashes, network
outages, and backend restarts.

## Motivation

The current `publish()` implementation is fail-fast: if the HTTP request fails,
the event is lost. For interactive chat with sub-30s responses, this is
acceptable — users can retry. For long-running tasks (minutes to hours), losing
the final result after expensive agent computation is unacceptable.

### Industry Precedent

| System | Approach |
|--------|----------|
| Datadog Agent | 15MB in-memory queue + disk spillover, persists in-flight transactions at shutdown |
| Fluentd | Memory or file buffer, 72h retry window, exponential backoff |
| Prometheus remote_write | WAL on disk, 2h retention, indefinite retry |
| Buildkite Agent | 10 retries with exponential backoff, 48h upload timeout |

## Design Goals

1. **Survive hub crashes** — queued events persist to disk and replay on restart
2. **Survive network outages** — retry with backoff until TTL expires
3. **Prioritize critical events** — `agent_response`/`agent_error` get more retries
4. **Minimal complexity** — use SQLite (Python stdlib), single-file database
5. **Bounded disk usage** — configurable max size, automatic cleanup
6. **At-least-once delivery** — events may be delivered twice; backend is idempotent

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         HubDaemon                               │
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │  Dispatcher  │───▶│ RelayClient  │───▶│  PublishQueue    │  │
│  │              │    │  .publish()  │    │  (SQLite)        │  │
│  └──────────────┘    └──────────────┘    └────────┬─────────┘  │
│                                                   │             │
│                      ┌────────────────────────────┘             │
│                      ▼                                          │
│              ┌──────────────────┐                               │
│              │ _queue_drain_loop│ (background task)             │
│              │ - retry pending  │                               │
│              │ - cleanup expired│                               │
│              └──────────────────┘                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ HTTP POST /publish
                    ┌──────────────────┐
                    │  Cloud Backend   │
                    │  (204 = ACK)     │
                    └──────────────────┘
```

## Data Model

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS publish_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    agent_message_id TEXT NOT NULL,     -- For deduplication and correlation
    event_json TEXT NOT NULL,
    event_type TEXT NOT NULL,           -- 'agent_response', 'agent_token', etc.
    priority INTEGER NOT NULL DEFAULT 0, -- 0=normal, 1=critical
    created_at REAL NOT NULL,           -- Unix timestamp
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL,       -- Per-event max retry limit
    next_retry_at REAL NOT NULL,        -- Unix timestamp
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_next_retry ON publish_queue(next_retry_at);
CREATE INDEX IF NOT EXISTS idx_priority ON publish_queue(priority DESC, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_agent_message ON publish_queue(agent_message_id, event_type);
```

### Event Priority

| Event Type | Priority | Max Retries | Rationale |
|------------|----------|-------------|-----------|
| `agent_response` | 1 (critical) | 20 | Final result — must deliver |
| `agent_error` | 1 (critical) | 20 | Final state — must deliver |
| `processing_status` | 1 (critical) | 20 | Completion signal |
| `task_submitted` | 0 (normal) | 5 | Nice-to-have, not essential |
| `agent_token` | 0 (normal) | 3 | Streaming tokens, stale quickly |
| `artifact_update` | 0 (normal) | 5 | Incremental, final response has full data |
| `task_status` | 0 (normal) | 5 | Status update, not final |

## Publish Flow

### Immediate Delivery Attempt

```python
async def publish(self, room_id: str, events: list[dict]) -> None:
    """Publish events with immediate retry, fallback to disk queue."""
    for event in events:
        event_type = event.get("type", "unknown")
        # agent_message_id is required for correlation; log warning if missing
        agent_message_id = event.get("agent_message_id")
        if not agent_message_id:
            logger.warning("Event missing agent_message_id — using event id or generating UUID")
            agent_message_id = event.get("id") or str(uuid.uuid4())
        
        priority = 1 if event_type in CRITICAL_EVENTS else 0
        max_immediate_retries = 3
        
        # Try immediate delivery
        delivered, error_type = await self._try_publish_with_retry(
            room_id, [event], max_retries=max_immediate_retries
        )
        
        if not delivered and error_type != "permanent":
            # Queue to disk for background retry (skip permanent 4xx errors)
            try:
                await self._queue.enqueue(room_id, agent_message_id, event, priority=priority)
                logger.warning(
                    "Queued %s event for background retry (room=%s, msg=%s)",
                    event_type, room_id[:8], agent_message_id[:8]
                )
            except Exception:
                logger.exception("Failed to queue event — dropping")
        # Never raise — let dispatch continue
```

### Retry Logic

Two different backoff strategies are used intentionally:

1. **Immediate retries** (in `_try_publish_with_retry`): Aggressive backoff starting at 1s
   (`2^attempt` = 1s, 2s, 4s...) because we want fast recovery for transient blips.

2. **Background retries** (in `_drain_pending_events`): Conservative backoff starting at 30s
   (`30 * 2^retry_count` = 30s, 60s, 120s...) because queued events already failed
   immediate delivery, so the issue is likely persistent (network down, backend overloaded).

```python
async def _try_publish_with_retry(
    self, room_id: str, events: list[dict], max_retries: int = 3
) -> tuple[bool, str | None]:
    """Attempt publish with exponential backoff.
    
    Returns (delivered, error_type):
    - (True, None) = success
    - (False, "permanent") = 4xx error, don't retry
    - (False, "transient") = network/5xx error, can retry
    
    Uses aggressive backoff (1s, 2s, 4s...) for immediate retries.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            client = await self._get_http_client()
            resp = await client.post(
                f"{self._base}/api/v1/relay/hub/{self._hub_id}/publish",
                json={"room_id": room_id, "events": events},
                headers={"X-API-Key": self._api_key},
            )
            resp.raise_for_status()
            return (True, None)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            last_exc = exc
            delay = min(2 ** attempt, 30)  # 1s, 2s, 4s... max 30s
            logger.warning(
                "Publish attempt %d/%d failed: %s — retrying in %ds",
                attempt + 1, max_retries, exc, delay
            )
            await asyncio.sleep(delay)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                last_exc = exc
                delay = min(2 ** attempt, 30)
                logger.warning(
                    "Publish got %d — retrying in %ds",
                    exc.response.status_code, delay
                )
                await asyncio.sleep(delay)
            else:
                # 4xx = permanent error, don't retry or queue
                logger.error("Publish rejected with %d: %s", 
                    exc.response.status_code, exc.response.text)
                return (False, "permanent")
    
    logger.error("Publish failed after %d attempts: %s", max_retries, last_exc)
    return (False, "transient")
```

### Background Queue Drain

```python
import json
import time

MAX_RETRY_DELAY = 3600  # 1 hour max between retries

async def _queue_drain_loop(self) -> None:
    """Background task to retry queued events."""
    interval = self.config.publish_queue.drain_interval
    while not self._should_stop:
        try:
            await self._drain_pending_events()
            await self._cleanup_expired_events()
        except Exception:
            logger.exception("Queue drain error")
        await asyncio.sleep(interval)

async def _drain_pending_events(self) -> None:
    """Process events ready for retry."""
    now = time.time()
    batch_size = self.config.publish_queue.drain_batch_size
    batch = await self._queue.get_ready_events(now, limit=batch_size)
    
    for event_id, room_id, agent_message_id, event_json, retry_count, max_retries in batch:
        # Check if max retries exceeded
        if retry_count >= max_retries:
            logger.warning(
                "Event %d exceeded max retries (%d) — dropping (msg=%s)",
                event_id, max_retries, agent_message_id[:8]
            )
            await self._queue.delete(event_id)
            continue
        
        event = json.loads(event_json)
        delivered, error_type = await self._try_publish_with_retry(
            room_id, [event], max_retries=1  # Single attempt per drain cycle
        )
        
        if delivered:
            await self._queue.delete(event_id)
            logger.info("Delivered queued event %d (msg=%s)", event_id, agent_message_id[:8])
        elif error_type == "permanent":
            # 4xx error — don't retry
            logger.warning("Event %d got permanent error — dropping", event_id)
            await self._queue.delete(event_id)
        else:
            # Schedule next retry with conservative exponential backoff
            # Starts at 30s (vs 1s for immediate) because issue is likely persistent
            delay = min(30 * (2 ** retry_count), MAX_RETRY_DELAY)
            await self._queue.update_retry(
                event_id, retry_count + 1, now + delay, str(error_type)
            )

async def _cleanup_expired_events(self) -> None:
    """Remove expired events and enforce size limits."""
    expired = await self._queue.cleanup_expired()
    if expired > 0:
        logger.info("Cleaned up %d expired events", expired)
    
    by_size = await self._queue.cleanup_by_size()
    if by_size > 0:
        logger.warning("Cleaned up %d events due to size limit", by_size)
```

## Configuration

Add to `HubConfig` using a nested model for cleaner YAML structure:

```python
from pydantic import BaseModel

class PublishQueueConfig(BaseModel):
    """Configuration for the disk-backed publish queue."""
    enabled: bool = True
    max_size_mb: int = 50           # Max disk usage for queue
    ttl_hours: int = 24             # Events older than this are dropped
    drain_interval: int = 30        # Seconds between drain cycles
    drain_batch_size: int = 20      # Events processed per drain cycle
    
    # Per-event-type retry limits (power user tuning)
    max_retries_critical: int = 20  # agent_response, agent_error, processing_status
    max_retries_normal: int = 5     # task_submitted, artifact_update, task_status
    max_retries_streaming: int = 3  # agent_token (stale quickly)


class HubConfig(BaseModel):
    # ... existing fields ...
    
    publish_queue: PublishQueueConfig = PublishQueueConfig()
```

### Configuration Guide for Power Users

| Scenario | Recommended Tuning |
|----------|-------------------|
| **10-50 agents** | Increase `max_size_mb` to 100-200 |
| **Frequent network issues** | Increase `max_size_mb`, decrease `drain_interval` to 10-15s |
| **Faster recovery needed** | Decrease `drain_interval` to 10-15s, increase `drain_batch_size` to 50 |
| **Very chatty agents (lots of tokens)** | Set `max_retries_streaming` to 1 or 0 |
| **Long-running tasks (hours)** | Increase `ttl_hours` to 48-72 |
| **Limited disk space** | Decrease `max_size_mb` to 20-30 |

### Example `config.yaml`

```yaml
# Default config (suitable for 1-10 agents)
publish_queue:
  enabled: true
  max_size_mb: 50
  ttl_hours: 24
  drain_interval: 30
  drain_batch_size: 20

# Power user config (50 agents, unreliable network)
publish_queue:
  enabled: true
  max_size_mb: 200
  ttl_hours: 48
  drain_interval: 10
  drain_batch_size: 50
  max_retries_streaming: 1  # Drop tokens faster
```

### Storage Location

```
~/.hybro/
├── config.yaml
├── hub_id
└── data/
    └── publish_queue.db    # SQLite database
```

## Lifecycle Integration

### Startup

```python
async def _startup(self) -> None:
    # ... existing startup ...
    
    # Initialize publish queue
    if self.config.publish_queue.enabled:
        queue_path = HYBRO_DIR / "data" / "publish_queue.db"
        self.relay.init_queue(queue_path, self.config.publish_queue)
        
        # Replay any events queued before last shutdown
        pending = await self.relay.get_queue_stats()
        if pending["total"] > 0:
            logger.info(
                "Found %d queued events from previous session — triggering immediate drain",
                pending["total"]
            )
            # Trigger immediate drain instead of waiting for first interval
            asyncio.create_task(self._drain_pending_events())
```

### Shutdown

```python
async def _shutdown(self) -> None:
    # ... existing shutdown ...
    
    # Flush in-flight publishes to queue before closing
    if self.config.publish_queue.enabled:
        stats = await self.relay.get_queue_stats()
        if stats["total"] > 0:
            logger.info(
                "Shutdown with %d events still queued — will retry on restart",
                stats["total"]
            )
        self.relay.close_queue()
```

## Complexity Evaluation

### Implementation Effort

| Component | Estimated Lines | Complexity |
|-----------|-----------------|------------|
| `PublishQueue` class (SQLite wrapper) | ~150 | Low |
| Retry logic in `RelayClient.publish()` | ~80 | Low |
| Background drain loop | ~60 | Low |
| Config additions | ~20 | Trivial |
| Lifecycle integration | ~30 | Low |
| Tests | ~200 | Medium |
| **Total** | **~540** | **Low-Medium** |

### Dependencies

- **SQLite**: Python stdlib (`sqlite3`), no new dependencies
- **aiosqlite** (optional): For async SQLite access. Can use sync SQLite with
  `run_in_executor()` to avoid adding a dependency.

### Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| SQLite corruption on crash | Low | Medium | Use WAL mode, periodic integrity checks |
| Disk fills up | Low | High | Configurable max size, automatic cleanup |
| Duplicate delivery | Medium | Low | Backend is idempotent (overwrites) |
| Stale events delivered | Medium | Low | TTL-based expiration (24h default) |
| Performance overhead | Low | Low | Async writes, batched reads |
| Thread safety issues | Medium | High | Use connection per thread or serialize access |
| Event ordering violated | Medium | Medium | Process events per-message in order (see below) |
| Max retries never enforced | Medium | Medium | Check retry_count against max_retries before retry |

### SQLite Reliability

SQLite is well-suited for this use case:
- **ACID guarantees** — events won't be partially written
- **WAL mode** — concurrent reads during writes, crash recovery
- **Single-file** — easy backup, no server process
- **Battle-tested** — used by Firefox, Chrome, iOS, Android for local storage

## Alternatives Considered

### 1. JSON Lines File

**Pros**: Simpler, append-only, human-readable  
**Cons**: No indexing, manual rotation, corruption on partial writes  
**Verdict**: Rejected — SQLite is nearly as simple with better guarantees

### 2. In-Memory Queue Only

**Pros**: Zero disk I/O, simplest implementation  
**Cons**: Lost on crash — defeats the purpose for long-running tasks  
**Verdict**: Rejected — doesn't solve the core problem

### 3. Redis/External Queue

**Pros**: Battle-tested, rich features  
**Cons**: External dependency, overkill for single-user daemon  
**Verdict**: Rejected — too heavy for the use case

### 4. Async File Writes (aiofiles)

**Pros**: Non-blocking  
**Cons**: No transactions, manual fsync for durability  
**Verdict**: Rejected — SQLite with `run_in_executor` is simpler

## Implementation Plan

### Phase 1: Core Queue (MVP)

1. Implement `PublishQueue` class with SQLite backend
2. Add retry logic to `RelayClient.publish()`
3. Add background drain loop to `HubDaemon`
4. Add config options
5. Write unit tests

**Estimated effort**: 2-3 days

### Phase 2: Hardening

1. Add queue size limits and automatic cleanup
2. Add metrics/logging for queue depth
3. Add CLI command to inspect/clear queue (`hybro-hub queue status`)
4. Integration tests with simulated network failures

**Estimated effort**: 1-2 days

### Phase 3: Observability (Optional)

1. Expose queue stats via local HTTP endpoint
2. Add Prometheus metrics (if hub grows to support it)

**Estimated effort**: 1 day

## Known Gaps and Edge Cases

### 1. Event Ordering

**Problem**: Events for the same `agent_message_id` must be delivered in order
(e.g., `task_submitted` before `agent_token` before `agent_response`). If
`task_submitted` fails and queues while `agent_token` succeeds, the user sees
tokens before "Agent X is working...".

**Mitigation**: For streaming events, ordering violations are cosmetic — the
final `agent_response` overwrites everything. For critical events, the backend
already handles out-of-order delivery gracefully (idempotent writes).

**Future enhancement**: Group events by `agent_message_id` and process in FIFO
order within each group. Not required for MVP.

### 2. Graceful Degradation at Call Sites

**Problem**: The design shows retry inside `publish()`, but doesn't address
what happens when `publish()` still raises after queueing (e.g., queue is full,
SQLite error).

**Mitigation**: `publish()` should catch queue errors and log them, never
raising to the caller. The dispatch loop must continue even if queueing fails.

```python
async def publish(self, room_id: str, events: list[dict]) -> None:
    for event in events:
        agent_message_id = event.get("agent_message_id", "unknown")
        # ... try immediate delivery ...
        if not delivered and error_type != "permanent":
            try:
                await self._queue.enqueue(room_id, agent_message_id, event, priority=priority)
            except Exception:
                logger.exception("Failed to queue event — dropping")
        # Never raise — let dispatch continue
```

### 3. Shutdown Race Condition

**Problem**: If the hub is shutting down while `_queue_drain_loop` is mid-retry,
events could be double-processed or the DB could be closed mid-write.

**Mitigation**: Use a shutdown flag and wait for the drain loop to complete
before closing the queue.

```python
async def _shutdown(self) -> None:
    self._should_stop = True
    if self._drain_task:
        self._drain_task.cancel()
        try:
            await self._drain_task
        except asyncio.CancelledError:
            pass
    self.relay.close_queue()
```

### 4. SQLite Thread Safety

**Problem**: `sqlite3.connect(..., check_same_thread=False)` allows multi-thread
access but doesn't make it safe. Concurrent writes can cause "database is locked"
errors.

**Mitigation options**:
- **Option A**: Use a dedicated thread with a queue for all DB operations
- **Option B**: Use `asyncio.Lock` to serialize access (simpler, slight perf hit)
- **Option C**: Use `aiosqlite` which handles this internally

**Recommendation**: Option B for MVP (simplest), migrate to `aiosqlite` if
performance becomes an issue.

```python
class PublishQueue:
    def __init__(self, ...):
        self._lock = asyncio.Lock()
    
    async def enqueue(self, ...) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._enqueue_sync, ...)
```

### 5. Max Retries Enforcement

**Problem**: The Event Priority table shows max retries per event type, but the
`_drain_pending_events` code doesn't check if `retry_count >= max_retries`.

**Mitigation**: Add max_retries to the schema and check before retrying.

```python
async def _drain_pending_events(self) -> None:
    batch = await self._queue.get_ready_events(now, limit=batch_size)
    for event_id, room_id, agent_message_id, event_json, retry_count, max_retries in batch:
        if retry_count >= max_retries:
            logger.warning("Event %d exceeded max retries — dropping", event_id)
            await self._queue.delete(event_id)
            continue
        # ... proceed with retry ...
```

### 6. 4xx Error Handling in Background Drain

**Problem**: The immediate retry logic returns `True` for 4xx errors (don't queue),
but the background drain doesn't distinguish 4xx from 5xx. A queued event that
later gets 403 (e.g., room deleted) will retry forever until TTL.

**Mitigation**: Return error type from `_try_publish_with_retry` and delete
events that get permanent 4xx errors.

```python
async def _try_publish_with_retry(...) -> tuple[bool, str | None]:
    """Returns (delivered, error_type). error_type is 'permanent' for 4xx."""
    # ...
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            return (False, "permanent")  # Don't retry
        # ...
    return (False, "transient")

# In drain loop:
delivered, error_type = await self._try_publish_with_retry(...)
if delivered:
    self._queue.delete(event_id)
elif error_type == "permanent":
    logger.warning("Event %d got permanent error — dropping", event_id)
    self._queue.delete(event_id)
else:
    # Schedule retry
```

### 7. WAL File Cleanup

**Problem**: SQLite WAL mode creates `-wal` and `-shm` files alongside the main
DB. These can grow large and aren't cleaned up until a checkpoint.

**Mitigation**: Periodically run `PRAGMA wal_checkpoint(TRUNCATE)` to reclaim
space, especially after `cleanup_expired()`.

```python
def cleanup_expired(self) -> int:
    # ... delete old events ...
    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return count
```

## Decision

**Recommendation**: Implement Phase 1 now if long-running tasks are on the
roadmap. The complexity is low (~540 lines), the risk is minimal (SQLite is
proven), and the benefit is significant (no lost results after expensive
agent work).

If long-running tasks are not imminent, defer to Phase 1 when:
- Agents that run >5 minutes are added
- Users report lost results after hub crashes
- "Background task" features are prioritized

## Appendix: Full PublishQueue Implementation Sketch

```python
"""Disk-backed publish queue using SQLite."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    agent_message_id TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_type TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL,
    next_retry_at REAL NOT NULL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_next_retry ON publish_queue(next_retry_at);
CREATE INDEX IF NOT EXISTS idx_priority ON publish_queue(priority DESC, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_agent_message ON publish_queue(agent_message_id, event_type);
"""

CRITICAL_EVENTS = frozenset({"agent_response", "agent_error", "processing_status"})
STREAMING_EVENTS = frozenset({"agent_token"})

# Default max retries by event category (overridable via config)
DEFAULT_MAX_RETRIES_CRITICAL = 20   # agent_response, agent_error, processing_status
DEFAULT_MAX_RETRIES_NORMAL = 5      # task_submitted, artifact_update, task_status
DEFAULT_MAX_RETRIES_STREAMING = 3   # agent_token


def get_max_retries(event_type: str, queue_config: "PublishQueueConfig") -> int:
    """Get max retries for an event type based on config."""
    if event_type in CRITICAL_EVENTS:
        return queue_config.max_retries_critical
    elif event_type in STREAMING_EVENTS:
        return queue_config.max_retries_streaming
    else:
        return queue_config.max_retries_normal


class PublishQueue:
    def __init__(
        self, 
        db_path: Path, 
        config: "PublishQueueConfig",
    ):
        self._db_path = db_path
        self._config = config
        self._max_size_bytes = config.max_size_mb * 1024 * 1024
        self._ttl_seconds = config.ttl_hours * 3600
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            # Checkpoint WAL before closing
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
            self._conn = None

    async def enqueue(
        self, room_id: str, agent_message_id: str, event: dict[str, Any], priority: int = 0
    ) -> int:
        """Thread-safe async enqueue."""
        async with self._lock:
            return await asyncio.to_thread(
                self._enqueue_sync, room_id, agent_message_id, event, priority
            )

    def _enqueue_sync(
        self, room_id: str, agent_message_id: str, event: dict[str, Any], priority: int
    ) -> int:
        now = time.time()
        event_type = event.get("type", "unknown")
        event_json = json.dumps(event)
        max_retries = get_max_retries(event_type, self._config)
        
        cursor = self._conn.execute(
            """
            INSERT INTO publish_queue 
            (room_id, agent_message_id, event_json, event_type, priority, 
             created_at, max_retries, next_retry_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (room_id, agent_message_id, event_json, event_type, priority, 
             now, max_retries, now),
        )
        self._conn.commit()
        return cursor.lastrowid

    async def get_ready_events(
        self, now: float, limit: int = 20
    ) -> list[tuple[int, str, str, str, int, int]]:
        """Get events ready for retry. Returns (id, room_id, agent_message_id, 
        event_json, retry_count, max_retries)."""
        async with self._lock:
            return await asyncio.to_thread(self._get_ready_events_sync, now, limit)

    def _get_ready_events_sync(
        self, now: float, limit: int
    ) -> list[tuple[int, str, str, str, int, int]]:
        cursor = self._conn.execute(
            """
            SELECT id, room_id, agent_message_id, event_json, retry_count, max_retries
            FROM publish_queue
            WHERE next_retry_at <= ?
            ORDER BY priority DESC, next_retry_at ASC
            LIMIT ?
            """,
            (now, limit),
        )
        return cursor.fetchall()

    async def update_retry(
        self, event_id: int, retry_count: int, next_retry_at: float, error: str | None = None
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_retry_sync, event_id, retry_count, next_retry_at, error
            )

    def _update_retry_sync(
        self, event_id: int, retry_count: int, next_retry_at: float, error: str | None
    ) -> None:
        self._conn.execute(
            """
            UPDATE publish_queue
            SET retry_count = ?, next_retry_at = ?, last_error = ?
            WHERE id = ?
            """,
            (retry_count, next_retry_at, error, event_id),
        )
        self._conn.commit()

    async def delete(self, event_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, event_id)

    def _delete_sync(self, event_id: int) -> None:
        self._conn.execute("DELETE FROM publish_queue WHERE id = ?", (event_id,))
        self._conn.commit()

    async def cleanup_expired(self) -> int:
        """Remove events older than TTL. Returns count deleted."""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_expired_sync)

    def _cleanup_expired_sync(self) -> int:
        cutoff = time.time() - self._ttl_seconds
        cursor = self._conn.execute(
            "DELETE FROM publish_queue WHERE created_at < ?", (cutoff,)
        )
        self._conn.commit()
        # Reclaim WAL space
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return cursor.rowcount

    async def cleanup_by_size(self) -> int:
        """Remove oldest events if DB exceeds max size. Returns count deleted."""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_by_size_sync)

    def _cleanup_by_size_sync(self) -> int:
        db_size = self._db_path.stat().st_size if self._db_path.exists() else 0
        if db_size <= self._max_size_bytes:
            return 0
        
        # Delete oldest 10% of events, prioritizing low-priority events
        total = self._conn.execute("SELECT COUNT(*) FROM publish_queue").fetchone()[0]
        to_delete = max(1, total // 10)
        
        cursor = self._conn.execute(
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
        self._conn.commit()
        # Note: VACUUM is expensive (rewrites entire DB) and holds the lock.
        # Only run if we deleted a significant portion (>20% of DB).
        # For routine cleanup, WAL checkpoint is sufficient.
        if to_delete > total * 0.2:
            self._conn.execute("VACUUM")
        return cursor.rowcount

    async def get_stats(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._get_stats_sync)

    def _get_stats_sync(self) -> dict[str, int]:
        cursor = self._conn.execute(
            """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN priority = 1 THEN 1 ELSE 0 END) as critical,
                SUM(CASE WHEN priority = 0 THEN 1 ELSE 0 END) as normal
            FROM publish_queue
            """
        )
        row = cursor.fetchone()
        return {"total": row[0], "critical": row[1] or 0, "normal": row[2] or 0}
```
