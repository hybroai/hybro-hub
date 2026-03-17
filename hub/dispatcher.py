"""Dispatcher — send A2A messages to local agents and translate responses.

Receives relay events, dispatches to local agents via A2A protocol,
and translates streaming responses into HubPublishEvent format for
the relay client to publish back.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from httpx_sse import aconnect_sse

from .agent_registry import LocalAgent

logger = logging.getLogger(__name__)

TERMINAL_FAILURE_STATES = {"failed", "rejected"}
INTERACTIVE_STATES = {"input-required", "auth-required"}
TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}


class DispatchEvent:
    """A translated event ready for relay publishing."""

    def __init__(self, type: str, agent_message_id: str, data: dict[str, Any]) -> None:
        self.type = type
        self.agent_message_id = agent_message_id
        self.data = data

    def to_publish_dict(self) -> dict:
        return {
            "type": self.type,
            "agent_message_id": self.agent_message_id,
            "data": self.data,
        }


@dataclass
class DispatchResult:
    """Structured result from a sync or streaming dispatch."""

    text: str = ""
    artifact_text: str = ""
    raw_parts: list[dict] = field(default_factory=list)
    task_state: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    error: str | None = None
    error_type: str | None = None


class Dispatcher:
    """Dispatches A2A messages to local agents."""

    def __init__(self, timeout: int = 120) -> None:
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def dispatch(
        self,
        agent: LocalAgent,
        message_dict: dict,
        agent_message_id: str,
        user_message_id: str | None = None,
    ) -> AsyncIterator[list[dict]]:
        """Dispatch an A2A message to a local agent, yielding event batches.

        The caller is responsible for publishing the initial ``task_submitted``
        event before iterating so the cloud UI shows immediate feedback.

        Streaming events (tokens, artifacts, status updates) are yielded
        individually as they arrive from the agent.  Terminal events
        (response/error + processing_status) are yielded as a final batch.

        Yields:
            Lists of HubPublishEvent dicts ready for relay.publish().
        """
        result = DispatchResult()

        try:
            if agent.agent_card.get("capabilities", {}).get("streaming"):
                async for event in self._dispatch_streaming(agent, message_dict, agent_message_id):
                    ev_dict = event.to_publish_dict()
                    if event.type in ("artifact_update", "task_status"):
                        yield [ev_dict]
                    if event.type == "artifact_update":
                        result.artifact_text += event.data.get("text", "")
                    elif event.type == "task_status":
                        result.task_state = event.data.get("state")
                        result.task_id = event.data.get("task_id") or result.task_id
                        result.context_id = event.data.get("context_id") or result.context_id
                    elif event.type == "task_submitted":
                        result.task_id = event.data.get("task_id") or result.task_id
                        result.context_id = event.data.get("context_id") or result.context_id

                if not result.artifact_text and not result.text and result.task_id:
                    result = await self._refetch_final_task(agent, result)
            else:
                result = await self._dispatch_sync(agent, message_dict)
        except Exception as exc:
            logger.error("Dispatch to %s failed: %s", agent.name, exc, exc_info=True)
            result.error = str(exc) or repr(exc) or "Unknown dispatch error"
            result.error_type = type(exc).__name__

        terminal: list[dict] = []
        self._emit_terminal_events(terminal, result, agent_message_id, user_message_id)
        yield terminal

    def _emit_terminal_events(
        self,
        events: list[dict],
        result: DispatchResult,
        agent_message_id: str,
        user_message_id: str | None,
    ) -> None:
        """Emit the terminal events (response/error + processing_status)."""
        if result.error:
            events.append(DispatchEvent(
                type="agent_error",
                agent_message_id=agent_message_id,
                data={
                    "error": result.error,
                    "error_type": result.error_type or "Unknown",
                },
            ).to_publish_dict())
            events.append(DispatchEvent(
                type="processing_status",
                agent_message_id=agent_message_id,
                data={"status": "failed", "user_message_id": user_message_id},
            ).to_publish_dict())
            return

        if result.task_state in TERMINAL_FAILURE_STATES:
            error_text = result.text or f"Agent task {result.task_state}"
            events.append(DispatchEvent(
                type="agent_error",
                agent_message_id=agent_message_id,
                data={
                    "error": error_text,
                    "error_type": "AgentTaskFailed",
                    "task_state": result.task_state,
                },
            ).to_publish_dict())
            events.append(DispatchEvent(
                type="processing_status",
                agent_message_id=agent_message_id,
                data={"status": "failed", "user_message_id": user_message_id},
            ).to_publish_dict())
            return

        if result.task_state in INTERACTIVE_STATES:
            events.append(DispatchEvent(
                type="task_interactive",
                agent_message_id=agent_message_id,
                data={
                    "state": result.task_state,
                    "task_id": result.task_id,
                    "context_id": result.context_id,
                    "status_text": result.text,
                },
            ).to_publish_dict())
            return

        response_text = result.artifact_text or result.text
        response_data: dict[str, Any] = {"content": response_text}
        if result.raw_parts:
            response_data["parts"] = result.raw_parts

        events.append(DispatchEvent(
            type="agent_response",
            agent_message_id=agent_message_id,
            data=response_data,
        ).to_publish_dict())
        events.append(DispatchEvent(
            type="processing_status",
            agent_message_id=agent_message_id,
            data={"status": "completed", "user_message_id": user_message_id},
        ).to_publish_dict())

    # ──── Sync dispatch (message/send) ────

    async def _refetch_final_task(
        self, agent: LocalAgent, result: DispatchResult,
    ) -> DispatchResult:
        """Re-fetch the completed task from the agent to get definitive response text.

        Called when streaming finished but both text accumulators are empty,
        mirroring the cloud path's task re-fetch on terminal status.
        """
        logger.info(
            "Streaming produced no text — re-fetching task %s from %s",
            result.task_id, agent.name,
        )
        try:
            client = await self._get_client()
            body = {
                "jsonrpc": "2.0",
                "id": uuid4().hex,
                "method": "tasks/get",
                "params": {"id": result.task_id},
            }
            resp = await client.post(
                agent.url, json=body,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            raw = resp.json()
            task_data = raw.get("result", raw)

            text, non_text = self._collect_parts_from_task(task_data)
            if text:
                logger.info("Re-fetch recovered %d chars from task %s", len(text), result.task_id)
                result.artifact_text = text
            if non_text:
                result.raw_parts = non_text

            state = task_data.get("status", {}).get("state")
            if state:
                result.task_state = state
        except Exception as exc:
            logger.warning(
                "Failed to re-fetch task %s from %s: %s (best-effort)",
                result.task_id, agent.name, exc,
            )
        return result

    async def _dispatch_sync(self, agent: LocalAgent, message_dict: dict) -> DispatchResult:
        """Send a synchronous A2A message/send request."""
        request_body = self._build_jsonrpc(message_dict, method="message/send")
        client = await self._get_client()

        resp = await client.post(
            agent.url,
            json=request_body,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        raw = resp.json()
        return self._extract_response_content(raw)

    # ──── Streaming dispatch (message/stream) ────

    async def _dispatch_streaming(
        self, agent: LocalAgent, message_dict: dict, agent_message_id: str,
    ) -> AsyncIterator[DispatchEvent]:
        """Send a streaming A2A message/stream request, yield classified events.

        Uses the ``kind`` discriminator per A2A spec RC v1.0 to classify
        each SSE event into typed publish events.
        """
        request_body = self._build_jsonrpc(message_dict, method="message/stream")
        client = await self._get_client()

        async with aconnect_sse(
            client, "POST", agent.url,
            json=request_body,
            headers={"Content-Type": "application/json"},
        ) as event_source:
            async for sse in event_source.aiter_sse():
                try:
                    data = json.loads(sse.data)
                except (json.JSONDecodeError, TypeError):
                    continue

                inner = data.get("result", data)
                kind = inner.get("kind", "")

                if kind == "artifact-update":
                    text = self._extract_artifact_text(inner)
                    raw_parts = self._collect_non_text_parts_from_artifact(inner)
                    yield DispatchEvent(
                        type="artifact_update",
                        agent_message_id=agent_message_id,
                        data={
                            "raw": inner,
                            "text": text,
                            "parts": raw_parts,
                            "append": inner.get("append", False),
                            "last_chunk": inner.get("lastChunk", inner.get("last_chunk", False)),
                        },
                    )
                elif kind == "status-update":
                    state = inner.get("status", {}).get("state")
                    text = self._extract_status_text(inner)
                    final = inner.get("final", False)
                    yield DispatchEvent(
                        type="task_status",
                        agent_message_id=agent_message_id,
                        data={
                            "state": state,
                            "status_text": text,
                            "final": final,
                            "task_id": inner.get("taskId", inner.get("task_id")),
                            "context_id": inner.get("contextId", inner.get("context_id")),
                            "raw": inner,
                        },
                    )
                elif kind == "task":
                    yield DispatchEvent(
                        type="task_submitted",
                        agent_message_id=agent_message_id,
                        data={
                            "task_id": inner.get("id"),
                            "context_id": inner.get("contextId", inner.get("context_id")),
                        },
                    )
                elif kind == "message":
                    text = self._extract_message_text(inner)
                    raw_parts = self._collect_non_text_parts_from_message(inner)
                    if text or raw_parts:
                        artifact_parts = []
                        if text:
                            artifact_parts.append({"kind": "text", "text": text})
                        artifact_parts.extend(raw_parts)
                        yield DispatchEvent(
                            type="artifact_update",
                            agent_message_id=agent_message_id,
                            data={
                                "raw": inner,
                                "text": text,
                                "parts": raw_parts,
                                "append": True,
                                "last_chunk": False,
                                "artifact": {
                                    "artifactId": f"{agent_message_id}-stream",
                                    "parts": artifact_parts,
                                },
                            },
                        )
                else:
                    if kind:
                        logger.warning("Unknown streaming event kind: %s", kind)

    # ──── JSON-RPC construction ────

    @staticmethod
    def _build_jsonrpc(message_dict: dict, method: str) -> dict:
        """Build a JSON-RPC 2.0 envelope for an A2A message."""
        return {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": {
                "message": message_dict,
            },
        }

    # ──── Response extraction ────

    @staticmethod
    def _collect_parts(parts_list: list[dict]) -> tuple[str, list[dict]]:
        """Separate text from non-text parts in a parts array.

        Returns (concatenated_text, non_text_parts_as_raw_dicts).
        """
        texts: list[str] = []
        non_text: list[dict] = []
        for p in parts_list:
            root = p.get("root", p)
            if "text" in root:
                texts.append(root["text"])
            else:
                non_text.append(p)
        return "".join(texts), non_text

    @classmethod
    def _extract_response_content(cls, result: dict) -> DispatchResult:
        """Extract structured content from a JSON-RPC A2A response.

        Uses the ``kind`` discriminator per A2A spec RC v1.0.
        """
        inner = result.get("result", result)
        kind = inner.get("kind", "")

        if kind == "task":
            state = inner.get("status", {}).get("state")
            text, non_text = cls._collect_parts_from_task(inner)
            return DispatchResult(
                text=text,
                artifact_text=text,
                raw_parts=non_text,
                task_state=state,
                task_id=inner.get("id"),
                context_id=inner.get("contextId", inner.get("context_id")),
            )

        if kind == "message":
            parts = inner.get("parts", [])
            text, non_text = cls._collect_parts(parts)
            return DispatchResult(text=text, raw_parts=non_text)

        # Fallback for agents that don't set kind (pre-spec)
        text, non_text = cls._collect_parts_from_task(inner)
        if not text:
            msg_parts = inner.get("parts", [])
            text, non_text = cls._collect_parts(msg_parts)

        return DispatchResult(
            text=text or str(inner),
            raw_parts=non_text,
            task_state=inner.get("status", {}).get("state") if "status" in inner else None,
        )

    @classmethod
    def _collect_parts_from_task(cls, task_dict: dict) -> tuple[str, list[dict]]:
        """Collect text and non-text parts from a Task dict.

        Many A2A agents duplicate the response in both ``status.message``
        and ``artifacts``.  To avoid doubled text, prefer artifact text
        when available and only fall back to status.message text.
        """
        all_non_text: list[dict] = []

        artifact_texts: list[str] = []
        for artifact in task_dict.get("artifacts", []):
            t, nt = cls._collect_parts(artifact.get("parts", []))
            if t:
                artifact_texts.append(t)
            all_non_text.extend(nt)

        if artifact_texts:
            return "".join(artifact_texts), all_non_text

        status_msg = task_dict.get("status", {}).get("message", {})
        if status_msg:
            t, nt = cls._collect_parts(status_msg.get("parts", []))
            all_non_text.extend(nt)
            if t:
                return t, all_non_text

        return "", all_non_text

    @staticmethod
    def _extract_artifact_text(inner: dict) -> str:
        """Extract text content from an artifact-update event."""
        artifact = inner.get("artifact", {})
        for p in artifact.get("parts", []):
            root = p.get("root", p)
            if "text" in root:
                return root["text"]
        return ""

    @staticmethod
    def _collect_non_text_parts_from_artifact(inner: dict) -> list[dict]:
        """Collect non-text parts from an artifact-update event."""
        artifact = inner.get("artifact", {})
        non_text: list[dict] = []
        for p in artifact.get("parts", []):
            root = p.get("root", p)
            if "text" not in root:
                non_text.append(p)
        return non_text

    @staticmethod
    def _extract_status_text(inner: dict) -> str:
        """Extract text from a status-update event's message."""
        msg = inner.get("status", {}).get("message", {})
        for p in msg.get("parts", []):
            root = p.get("root", p)
            if "text" in root:
                return root["text"]
        return ""

    @staticmethod
    def _extract_message_text(inner: dict) -> str:
        """Extract text from a message event."""
        for p in inner.get("parts", []):
            root = p.get("root", p)
            if "text" in root:
                return root["text"]
        return ""

    @staticmethod
    def _collect_non_text_parts_from_message(inner: dict) -> list[dict]:
        """Collect non-text parts from a message event."""
        non_text: list[dict] = []
        for p in inner.get("parts", []):
            root = p.get("root", p)
            if "text" not in root:
                non_text.append(p)
        return non_text
