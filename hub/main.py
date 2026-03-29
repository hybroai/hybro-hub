"""Hub daemon main loop.

Orchestrates: config loading, relay registration, agent discovery,
SSE subscription, event dispatch, and periodic re-sync.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any
from uuid import uuid4

import httpx

from .agent_registry import AgentRegistry
from .config import HYBRO_DIR, HubConfig
from .dispatcher import Dispatcher
from .privacy_router import PrivacyRouter
from .relay_client import RelayClient

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 60
RESYNC_INTERVAL = 120

CONNECTION_ERROR_TYPES = frozenset({
    "ConnectError",
    "ConnectTimeout",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "ConnectionAbortedError",
})


class HubDaemon:
    """The hub daemon orchestrator."""

    def __init__(self, config: HubConfig) -> None:
        self.config = config
        self.relay = RelayClient(
            gateway_url=config.cloud.gateway_url,
            hub_id=config.hub_id,
            api_key=config.cloud.api_key or "",
        )
        self.registry = AgentRegistry(config)
        self.dispatcher = Dispatcher(timeout=config.dispatcher_read_timeout_secs)
        self.privacy = PrivacyRouter(
            sensitive_keywords=config.privacy.sensitive_keywords,
            sensitive_patterns=config.privacy.sensitive_patterns,
        )
        self._shutdown_event = asyncio.Event()
        self._last_sync_payload: list[dict] | None = None
        self._drain_task: asyncio.Task | None = None
        self._startup_drain_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None

    async def run(self) -> None:
        """Main entry point — run the hub daemon."""
        loop = asyncio.get_running_loop()
        self._run_task = asyncio.current_task()

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._signal_shutdown)
        except NotImplementedError:
            signal.signal(signal.SIGINT, lambda *_: self._signal_shutdown())

        try:
            await self._startup()
            await self._event_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    # ──── Startup ────

    async def _startup(self) -> None:
        logger.info("Starting hub daemon (hub_id=%s)", self.config.hub_id)

        # Initialise the disk-backed publish queue first so that any publish
        # failures during startup (registration, sync) can fall back to the queue.
        if self.config.publish_queue.enabled:
            queue_path = HYBRO_DIR / "data" / "publish_queue.db"
            self.relay.init_queue(queue_path, self.config.publish_queue)
            stats = await self.relay.get_queue_stats()
            if stats["total"] > 0:
                logger.info(
                    "Found %d queued events from previous session — "
                    "triggering immediate drain",
                    stats["total"],
                )
                # Drain immediately rather than waiting for first interval
                self._startup_drain_task = asyncio.create_task(
                    self.relay.drain_queued_events(
                        batch_size=self.config.publish_queue.drain_batch_size
                    )
                )
                self._startup_drain_task.add_done_callback(
                    lambda t: t.exception() if not t.cancelled() else None
                )

        # Discover local agents BEFORE registering with the relay.
        # Registering first opens the message channel immediately, but the
        # in-memory registry is still empty at that point — any message the
        # cloud routes during the discovery window hits "agent not found".
        # By discovering and syncing first, the hub only announces itself
        # once it is fully ready to serve traffic (ready-before-register).
        agents = await self.registry.discover()
        if not agents:
            logger.warning(
                "No local agents found. Start an A2A agent and it will be "
                "discovered automatically."
            )

        # Register with relay
        try:
            await self.relay.register()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                logger.error(
                    "Hub registration failed: invalid API key (401). "
                    "Run: hybro-hub start --api-key hybro_..."
                )
            else:
                logger.error(
                    "Hub registration failed with HTTP %d: %s",
                    status, exc.response.text,
                )
            raise SystemExit(1)
        except httpx.ConnectError as exc:
            logger.error(
                "Hub registration failed: cannot reach %s (%s)",
                self.config.cloud.gateway_url, exc,
            )
            raise SystemExit(1)

        # Sync agents to cloud
        await self._sync_agents()

        logger.info(
            "Hub ready — %d agent(s) synced. Waiting for messages...",
            len(self.registry.get_healthy_agents()),
        )

    # ──── Event loop ────

    async def _event_loop(self) -> None:
        """Subscribe to relay SSE and dispatch events."""
        # Start background tasks
        health_task = asyncio.create_task(self._health_check_loop())
        resync_task = asyncio.create_task(self._resync_loop())
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        background_tasks = [health_task, resync_task, heartbeat_task]

        if self.config.publish_queue.enabled:
            self._drain_task = asyncio.create_task(self._queue_drain_loop())
            background_tasks.append(self._drain_task)

        try:
            async for event in self.relay.subscribe():
                if self._shutdown_event.is_set():
                    break
                if event.get("type") == "_connected":
                    self._last_sync_payload = None
                    await self._sync_agents()
                    continue
                try:
                    await self._handle_event(event)
                except Exception:
                    logger.exception("Failed to handle relay event")
        finally:
            for t in background_tasks:
                t.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Handle a relay event based on its type."""
        event_type = event.get("type", "user_message")

        if event_type == "cancel_task":
            await self._handle_cancel_task(event)
            return

        if event_type == "user_reply":
            await self._handle_user_reply(event)
            return

        if event_type != "user_message":
            logger.warning("Unhandled relay event type: %s", event_type)
            return

        await self._handle_user_message(event)

    async def _handle_user_message(self, event: dict[str, Any]) -> None:
        """Handle a user_message relay event."""
        local_agent_id = event.get("local_agent_id")
        room_id = event.get("room_id")
        agent_message_id = event.get("agent_message_id")
        user_message_id = event.get("user_message_id")
        message_dict = event.get("message")

        if not all([local_agent_id, room_id, agent_message_id, message_dict]):
            logger.warning("Incomplete relay event: %s", event)
            if room_id and agent_message_id:
                await self._publish_failure(
                    room_id, agent_message_id,
                    error="Incomplete relay event received by hub",
                    error_type="InvalidEvent",
                    user_message_id=user_message_id,
                )
            return

        agent = self.registry.get_agent(local_agent_id)
        if not agent:
            logger.error("No agent found for local_agent_id=%s", local_agent_id)
            await self._publish_failure(
                room_id, agent_message_id,
                error=f"Agent '{local_agent_id}' is no longer available on this hub",
                error_type="AgentNotFound",
                user_message_id=user_message_id,
            )
            return

        text = _extract_text(message_dict)
        self.privacy.check_and_log(text, agent.name)

        logger.info(
            "Dispatching to %s (room=%s, msg=%s)",
            agent.name, room_id, agent_message_id[:8],
        )

        await self.relay.publish(room_id, [{
            "type": "task_submitted",
            "agent_message_id": agent_message_id,
            "data": {"task_id": uuid4().hex, "agent_name": agent.name},
        }])

        dispatch_error_type: str | None = None
        async for batch in self.dispatcher.dispatch(
            agent=agent,
            message_dict=message_dict,
            agent_message_id=agent_message_id,
            user_message_id=user_message_id,
        ):
            await self.relay.publish(room_id, batch)
            for ev in batch:
                if ev.get("type") == "agent_error":
                    dispatch_error_type = ev.get("data", {}).get("error_type")

        if dispatch_error_type and dispatch_error_type in CONNECTION_ERROR_TYPES:
            agent_name = agent.name
            if self.registry.remove_agent(local_agent_id):
                logger.warning(
                    "Dispatch to %s failed with %s — removed from registry, syncing",
                    agent_name, dispatch_error_type,
                )
                self._last_sync_payload = None
                await self._sync_agents()

    async def _handle_cancel_task(self, event: dict[str, Any]) -> None:
        """Best-effort cancellation of an in-flight task on a local agent."""
        local_agent_id = event.get("local_agent_id")
        agent_message_id = event.get("agent_message_id")
        task_id = event.get("task_id")

        agent = self.registry.get_agent(local_agent_id) if local_agent_id else None
        if not agent:
            logger.warning(
                "cancel_task for unknown agent %s (msg=%s)",
                local_agent_id, agent_message_id,
            )
            return

        if not task_id:
            logger.info("cancel_task without task_id — cannot forward to agent")
            return

        logger.info("Forwarding cancel_task to %s (task=%s)", agent.name, task_id)
        try:
            await self.dispatcher.cancel_task(agent, task_id)
        except Exception:
            logger.warning("Failed to cancel task on %s (best-effort)", agent.name, exc_info=True)

    async def _handle_user_reply(self, event: dict[str, Any]) -> None:
        """Handle a HITL reply by dispatching it to the local agent."""
        local_agent_id = event.get("local_agent_id")
        room_id = event.get("room_id")
        agent_message_id = event.get("agent_message_id")
        reply_text = event.get("reply_text", "")
        task_id = event.get("task_id")
        context_id = event.get("context_id")

        if not all([local_agent_id, room_id, agent_message_id]):
            logger.warning("Incomplete user_reply event: %s", event)
            if room_id and agent_message_id:
                await self._publish_failure(
                    room_id, agent_message_id,
                    error="Incomplete user_reply event received by hub",
                    error_type="InvalidEvent",
                )
            return

        agent = self.registry.get_agent(local_agent_id)
        if not agent:
            logger.error("No agent found for local_agent_id=%s", local_agent_id)
            await self._publish_failure(
                room_id, agent_message_id,
                error=f"Agent '{local_agent_id}' is no longer available on this hub",
                error_type="AgentNotFound",
            )
            return

        reply_message: dict[str, Any] = {
            "role": "user",
            "parts": [{"kind": "text", "text": reply_text}],
            "messageId": uuid4().hex,
        }
        if task_id:
            reply_message["taskId"] = task_id
            reply_message["referenceTaskIds"] = [task_id]
        if context_id:
            reply_message["contextId"] = context_id

        logger.info(
            "Dispatching HITL reply to %s (room=%s, msg=%s)",
            agent.name, room_id, agent_message_id[:8],
        )

        await self.relay.publish(room_id, [{
            "type": "task_submitted",
            "agent_message_id": agent_message_id,
            "data": {"task_id": uuid4().hex, "agent_name": agent.name},
        }])

        dispatch_error_type: str | None = None
        async for batch in self.dispatcher.dispatch(
            agent=agent,
            message_dict=reply_message,
            agent_message_id=agent_message_id,
        ):
            await self.relay.publish(room_id, batch)
            for ev in batch:
                if ev.get("type") == "agent_error":
                    dispatch_error_type = ev.get("data", {}).get("error_type")

        if dispatch_error_type and dispatch_error_type in CONNECTION_ERROR_TYPES:
            agent_name = agent.name
            if self.registry.remove_agent(local_agent_id):
                logger.warning(
                    "HITL dispatch to %s failed with %s — removed from registry, syncing",
                    agent_name, dispatch_error_type,
                )
                self._last_sync_payload = None
                await self._sync_agents()

    # ──── Failure publishing ────

    async def _publish_failure(
        self,
        room_id: str,
        agent_message_id: str,
        error: str,
        error_type: str = "HubError",
        user_message_id: str | None = None,
    ) -> None:
        """Publish agent_error + processing_status=failed back to the relay."""
        await self.relay.publish(room_id, [
            {
                "type": "agent_error",
                "agent_message_id": agent_message_id,
                "data": {"error": error, "error_type": error_type},
            },
            {
                "type": "processing_status",
                "agent_message_id": agent_message_id,
                "data": {"status": "failed", "user_message_id": user_message_id},
            },
        ])

    # ──── Background tasks ────

    async def _queue_drain_loop(self) -> None:
        """Periodically retry events that failed immediate delivery."""
        qcfg = self.config.publish_queue
        while not self._shutdown_event.is_set():
            await asyncio.sleep(qcfg.drain_interval)
            try:
                await self.relay.drain_queued_events(batch_size=qcfg.drain_batch_size)
                if self.relay._queue:
                    expired = await self.relay._queue.cleanup_expired()
                    by_size = await self.relay._queue.cleanup_by_size()
                    if expired:
                        logger.info("Queue: cleaned up %d expired events", expired)
                    if by_size:
                        logger.warning("Queue: cleaned up %d events (size limit)", by_size)
            except Exception:
                logger.exception("Queue drain error")

    async def _health_check_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            try:
                prev_count = len(self.registry.agents)
                await self.registry.health_check()
                if len(self.registry.agents) < prev_count:
                    logger.info("Agents pruned by health check — triggering re-sync")
                    self._last_sync_payload = None
                    await self._sync_agents()
            except Exception:
                logger.exception("Health check failed")

    async def _resync_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(RESYNC_INTERVAL)
            try:
                await self.registry.discover()
                await self._sync_agents()
            except Exception:
                logger.exception("Agent re-sync failed")

    async def _heartbeat_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(self.config.heartbeat_interval)
            try:
                await self.relay.heartbeat()
            except Exception:
                logger.debug("Heartbeat failed (will retry next cycle)", exc_info=True)

    async def _sync_agents(self) -> None:
        payload = self.registry.to_sync_payload()
        if payload == self._last_sync_payload:
            return
        synced = await self.relay.sync_agents(payload)
        self._last_sync_payload = payload
        logger.info("Synced %d agents to cloud", len(synced))

    # ──── Shutdown ────

    def _signal_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self._shutdown_event.set()
        # Cancel the main task to unblock any awaits (e.g., SSE read)
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()

    async def _shutdown(self) -> None:
        logger.info("Shutting down hub daemon...")

        # Cancel the startup drain task if it's still running
        if self._startup_drain_task and not self._startup_drain_task.done():
            self._startup_drain_task.cancel()
            try:
                await self._startup_drain_task
            except asyncio.CancelledError:
                pass

        if self.config.publish_queue.enabled:
            stats = await self.relay.get_queue_stats()
            if stats["total"] > 0:
                logger.info(
                    "Shutdown with %d events still queued — will retry on next start",
                    stats["total"],
                )
            self.relay.close_queue()

        await self.relay.close()
        await self.registry.close()
        await self.dispatcher.close()
        logger.info("Hub daemon stopped.")


def _extract_text(message_dict: dict) -> str:
    """Extract text from an A2A Message dict."""
    parts = message_dict.get("parts", [])
    texts = []
    for p in parts:
        root = p.get("root", p)
        if "text" in root:
            texts.append(root["text"])
    return " ".join(texts)
