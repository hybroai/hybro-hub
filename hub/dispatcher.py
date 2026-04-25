"""Dispatcher — send A2A messages to local agents and translate responses.

Receives relay events, dispatches to local agents via A2A protocol,
and translates streaming responses into HubPublishEvent format for
the relay client to publish back.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from httpx_sse import aconnect_sse

from . import a2a_compat
from .a2a_compat import A2AVersionFallbackError, ResolvedInterface
from .agent_registry import LocalAgent

logger = logging.getLogger(__name__)

TERMINAL_FAILURE_STATES = {"failed", "rejected"}
INTERACTIVE_STATES = {"input-required", "auth-required"}
TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}
NON_TERMINAL_STATES = {"submitted", "working"}

POLL_REQUEST_TIMEOUT = 15.0  # seconds; short timeout per tasks/get poll request


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
    stream_emitted_content: bool = False


class Dispatcher:
    """Dispatches A2A messages to local agents.

    Currently supports two dispatch strategies: streaming (SSE via
    message/stream) and sync (blocking message/send with polling fallback).
    Both assume tasks complete within minutes.

    TODO(long-running): For tasks lasting hours, this architecture needs:
      1. Exponential backoff in _poll_until_terminal (replace fixed interval
         with min/max/multiplier, e.g. 2s→4s→8s...cap 60s).
      2. Per-agent dispatch strategy based on agent_card capabilities
         (e.g. skip blocking=True for agents declaring longRunning).
      3. Persist in-flight (task_id, agent_url, agent_message_id) to the
         publish queue DB so polling survives hub daemon restarts.
      4. Yield intermediate task_status events during polling so the UI
         shows progress for long waits.
    """

    def __init__(self, timeout: int = 300) -> None:
        self._read_timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=self._read_timeout,
                    write=30.0,
                    pool=5.0,
                )
            )
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

        If v1.0 dispatch fails with a fallback-eligible JSON-RPC error,
        retries once with v0.3 wire format using agent.fallback_interface.
        """
        result = DispatchResult()
        interface = agent.interface

        try:
            async for batch in self._dispatch_with_interface(
                agent, message_dict, agent_message_id, interface, result,
            ):
                yield batch
        except A2AVersionFallbackError as fallback_exc:
            if agent.fallback_interface:
                logger.warning(
                    "v1.0 dispatch to %s failed (%s) — retrying with v0.3",
                    agent.name, fallback_exc,
                )
                result = DispatchResult()
                try:
                    async for batch in self._dispatch_with_interface(
                        agent, message_dict, agent_message_id,
                        agent.fallback_interface, result,
                    ):
                        yield batch
                except Exception as exc:
                    logger.error(
                        "Fallback dispatch to %s also failed: %s",
                        agent.name, exc, exc_info=True,
                    )
                    result = DispatchResult()
                    result.error = str(fallback_exc) or repr(fallback_exc)
                    result.error_type = "A2AVersionFallback"
            else:
                result.error = str(fallback_exc) or repr(fallback_exc)
                result.error_type = "A2AVersionFallback"
        except Exception as exc:
            logger.error("Dispatch to %s failed: %s", agent.name, exc, exc_info=True)
            result.error = str(exc) or repr(exc) or "Unknown dispatch error"
            result.error_type = type(exc).__name__

        terminal: list[dict] = []
        self._emit_terminal_events(
            terminal,
            result,
            agent_message_id,
            user_message_id,
            agent.local_agent_id,
            stream_emitted_content=result.stream_emitted_content,
        )
        yield terminal

    async def _dispatch_with_interface(
        self,
        agent: LocalAgent,
        message_dict: dict,
        agent_message_id: str,
        interface: ResolvedInterface,
        result: DispatchResult,
    ) -> AsyncIterator[list[dict]]:
        """Core dispatch logic using a specific interface. Populates result in-place."""
        if agent.agent_card.get("capabilities", {}).get("streaming"):
            async for event in self._dispatch_streaming(agent, message_dict, agent_message_id, interface):
                ev_dict = event.to_publish_dict()
                if event.type in ("artifact_update", "task_status"):
                    yield [ev_dict]
                if event.type == "artifact_update":
                    # Streaming already emitted visible output for this message.
                    # Suppress the final synthetic agent_response event later to
                    # avoid duplicate rendering in downstream consumers.
                    if event.data.get("text") or event.data.get("parts"):
                        result.stream_emitted_content = True
                    result.artifact_text += event.data.get("text", "")
                elif event.type == "task_status":
                    result.task_state = event.data.get("state")
                    result.task_id = event.data.get("task_id") or result.task_id
                    result.context_id = event.data.get("context_id") or result.context_id
                elif event.type == "task_submitted":
                    result.task_id = event.data.get("task_id") or result.task_id
                    result.context_id = event.data.get("context_id") or result.context_id

            if not result.artifact_text and result.task_id:
                result_copy = await self._refetch_final_task(agent, result, interface=interface)
                result.artifact_text = result_copy.artifact_text
                result.raw_parts = result_copy.raw_parts
                result.task_state = result_copy.task_state or result.task_state
        else:
            sync_result = await self._dispatch_sync(agent, message_dict, interface)
            result.text = sync_result.text
            result.artifact_text = sync_result.artifact_text
            result.raw_parts = sync_result.raw_parts
            result.task_state = sync_result.task_state
            result.task_id = sync_result.task_id
            result.context_id = sync_result.context_id
            if result.task_state in NON_TERMINAL_STATES and result.task_id:
                polled = await self._poll_until_terminal(agent, result, interface=interface)
                result.text = polled.text
                result.artifact_text = polled.artifact_text
                result.raw_parts = polled.raw_parts
                result.task_state = polled.task_state
                result.error = polled.error
                result.error_type = polled.error_type

    def _emit_terminal_events(
        self,
        events: list[dict],
        result: DispatchResult,
        agent_message_id: str,
        user_message_id: str | None,
        agent_id: str | None = None,
        stream_emitted_content: bool = False,
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
            events.append(DispatchEvent(
                type="processing_status",
                agent_message_id=agent_message_id,
                data={"status": "input_required", "user_message_id": user_message_id},
            ).to_publish_dict())
            return

        artifact_text = (result.artifact_text or "").strip()
        terminal_text = (result.text or "").strip()
        # Prefer the richer text payload. Streaming artifact text can be partial
        # for some agents; when terminal text is longer, use it to avoid trimmed
        # prefixes in the final response.
        response_text = terminal_text if len(terminal_text) > len(artifact_text) else (artifact_text or terminal_text)

        # Streaming path already emitted artifact/message content incrementally.
        # If terminal text doesn't add anything richer, suppress agent_response
        # and keep only processing_status to avoid duplicate rendering.
        if stream_emitted_content and (not terminal_text or len(terminal_text) <= len(artifact_text)):
            events.append(DispatchEvent(
                type="processing_status",
                agent_message_id=agent_message_id,
                data={"status": "completed", "user_message_id": user_message_id},
            ).to_publish_dict())
            return

        response_data: dict[str, Any] = {"content": response_text}
        if agent_id is not None:
            response_data["agent_id"] = agent_id
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

    # ──── Cancel ────

    async def cancel_task(self, agent: LocalAgent, task_id: str) -> None:
        """Best-effort cancellation of an in-flight task on a local agent.

        Uses _check_response for JSON-RPC error handling and retries on
        fallback interface if the primary returns a version-mismatch error.
        """
        iface = agent.interface
        try:
            await self._do_cancel_task(agent, task_id, iface)
        except A2AVersionFallbackError:
            fb = agent.fallback_interface
            if fb and fb != iface:
                logger.info(
                    "CancelTask on %s failed with version error — retrying on fallback %s",
                    iface.url, fb.url,
                )
                await self._do_cancel_task(agent, task_id, fb)
            else:
                raise

    async def _do_cancel_task(
        self, agent: LocalAgent, task_id: str,
        iface: ResolvedInterface,
    ) -> None:
        """Low-level cancel on a specific interface."""
        version = iface.protocol_version
        method = a2a_compat.get_method_name("cancel_task", version)
        headers = {"Content-Type": "application/json", **a2a_compat.get_headers(version)}
        client = await self._get_client()
        body = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": {"id": task_id},
        }
        resp = await client.post(iface.url, json=body, headers=headers)
        self._check_response(resp)
        logger.info("Cancel response from %s: %d", agent.name, resp.status_code)

    # ──── Sync dispatch (message/send) ────

    async def _fetch_task(
        self, agent: LocalAgent, task_id: str,
        timeout: float | None = None,
        interface: ResolvedInterface | None = None,
    ) -> dict:
        """Fetch a task by ID via tasks/get JSON-RPC call.

        If the primary interface returns a fallback-eligible error AND the
        agent has a fallback_interface, retries GetTask on the fallback
        rather than bubbling up A2AVersionFallbackError (which would cause
        dispatch() to resend the entire message).
        """
        iface = interface or agent.interface
        try:
            return await self._do_fetch_task(agent, task_id, iface, timeout)
        except A2AVersionFallbackError:
            fb = agent.fallback_interface
            if fb and fb != iface:
                logger.info(
                    "GetTask on %s failed with version error — retrying on fallback %s",
                    iface.url, fb.url,
                )
                return await self._do_fetch_task(agent, task_id, fb, timeout)
            raise

    async def _do_fetch_task(
        self, agent: LocalAgent, task_id: str,
        iface: ResolvedInterface,
        timeout: float | None = None,
    ) -> dict:
        """Low-level task fetch on a specific interface."""
        version = iface.protocol_version
        method = a2a_compat.get_method_name("get_task", version)
        headers = {"Content-Type": "application/json", **a2a_compat.get_headers(version)}
        client = await self._get_client()
        body = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": {"id": task_id},
        }
        kwargs: dict[str, Any] = {"json": body, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(
                connect=10.0, read=timeout, write=30.0, pool=5.0,
            )
        resp = await client.post(iface.url, **kwargs)
        raw = self._check_response(resp)
        normalized = a2a_compat.extract_response(raw, version)
        return normalized.get("result", normalized)

    async def _refetch_final_task(
        self, agent: LocalAgent, result: DispatchResult,
        interface: ResolvedInterface | None = None,
    ) -> DispatchResult:
        """Re-fetch the completed task from the agent to get definitive response text."""
        logger.info(
            "Streaming produced no text — re-fetching task %s from %s",
            result.task_id, agent.name,
        )
        try:
            task_data = await self._fetch_task(agent, result.task_id, interface=interface)

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

    async def _poll_until_terminal(
        self,
        agent: LocalAgent,
        result: DispatchResult,
        poll_interval: float = 2.0,
        max_attempts: int = 30,
        interface: ResolvedInterface | None = None,
    ) -> DispatchResult:
        """Poll tasks/get until the task reaches a terminal or interactive state."""
        logger.info(
            "Sync dispatch returned non-terminal state '%s' — polling task %s on %s",
            result.task_state, result.task_id, agent.name,
        )

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            task_data = await self._fetch_task(
                agent, result.task_id, timeout=POLL_REQUEST_TIMEOUT,
                interface=interface,
            )

            state = task_data.get("status", {}).get("state")
            if state:
                result.task_state = state

            text, non_text = self._collect_parts_from_task(task_data)
            if text:
                result.artifact_text = text
                result.text = text
            if non_text:
                result.raw_parts = non_text

            result.context_id = (
                task_data.get("contextId", task_data.get("context_id"))
                or result.context_id
            )

            if result.task_state in TERMINAL_STATES | INTERACTIVE_STATES:
                logger.info(
                    "Polling task %s reached state '%s' after %d attempt(s)",
                    result.task_id, result.task_state, attempt + 1,
                )
                break
        else:
            logger.error(
                "Polling task %s on %s exhausted %d attempts (still '%s')",
                result.task_id, agent.name, max_attempts, result.task_state,
            )
            result.error = (
                f"Agent task {result.task_id} did not reach a terminal state "
                f"within {max_attempts} polling attempts"
            )
            result.error_type = "PollingTimeout"

        return result

    async def _dispatch_sync(
        self, agent: LocalAgent, message_dict: dict,
        interface: ResolvedInterface | None = None,
    ) -> DispatchResult:
        """Send a synchronous A2A send request with blocking=True."""
        iface = interface or agent.interface
        version = iface.protocol_version
        configuration: dict[str, Any] = {"blocking": True}
        request_body = self._build_jsonrpc(
            message_dict, base_method="send", version=version, configuration=configuration,
        )
        client = await self._get_client()
        headers = {"Content-Type": "application/json", **a2a_compat.get_headers(version)}

        resp = await client.post(iface.url, json=request_body, headers=headers)
        raw = self._check_response(resp)
        normalized = a2a_compat.extract_response(raw, version)
        return self._extract_response_content(normalized)

    # ──── Streaming dispatch (message/stream) ────

    async def _dispatch_streaming(
        self, agent: LocalAgent, message_dict: dict, agent_message_id: str,
        interface: ResolvedInterface | None = None,
    ) -> AsyncIterator[DispatchEvent]:
        """Send a streaming A2A stream request, yield classified events."""
        iface = interface or agent.interface
        version = iface.protocol_version
        request_body = self._build_jsonrpc(
            message_dict, base_method="stream", version=version,
        )
        client = await self._get_client()
        headers = {"Content-Type": "application/json", **a2a_compat.get_headers(version)}

        async with aconnect_sse(
            client, "POST", iface.url,
            json=request_body,
            headers=headers,
        ) as event_source:
            first_event = True
            async for sse in event_source.aiter_sse():
                try:
                    data = json.loads(sse.data)
                except (json.JSONDecodeError, TypeError):
                    continue

                if first_event:
                    first_event = False
                    err = a2a_compat.extract_jsonrpc_error(data)
                    if err:
                        if err.code in a2a_compat.FALLBACK_ELIGIBLE_CODES:
                            raise A2AVersionFallbackError(
                                f"JSON-RPC error {err.code}: {err.message}"
                            )
                        raise RuntimeError(
                            f"JSON-RPC error {err.code}: {err.message}"
                        )

                inner = data.get("result", data)
                classified = a2a_compat.classify_stream_event(inner, version)

                if classified is None:
                    if inner.get("kind", ""):
                        logger.warning("Unknown streaming event kind: %s", inner.get("kind"))
                    continue

                event_type, payload = classified

                if event_type == "artifact-update":
                    text = self._extract_artifact_text(payload)
                    raw_parts = self._collect_non_text_parts_from_artifact(payload)
                    yield DispatchEvent(
                        type="artifact_update",
                        agent_message_id=agent_message_id,
                        data={
                            "raw": payload,
                            "text": text,
                            "parts": raw_parts,
                            "append": payload.get("append", False),
                            "last_chunk": payload.get("lastChunk", payload.get("last_chunk", False)),
                        },
                    )
                elif event_type == "status-update":
                    state = payload.get("status", {}).get("state")
                    text = self._extract_status_text(payload)
                    final = payload.get("final", False)
                    yield DispatchEvent(
                        type="task_status",
                        agent_message_id=agent_message_id,
                        data={
                            "state": state,
                            "status_text": text,
                            "final": final,
                            "task_id": payload.get("taskId", payload.get("task_id")),
                            "context_id": payload.get("contextId", payload.get("context_id")),
                            "raw": payload,
                        },
                    )
                elif event_type == "task":
                    yield DispatchEvent(
                        type="task_submitted",
                        agent_message_id=agent_message_id,
                        data={
                            "task_id": payload.get("id"),
                            "context_id": payload.get("contextId", payload.get("context_id")),
                        },
                    )
                elif event_type == "message":
                    text = self._extract_message_text(payload)
                    raw_parts = self._collect_non_text_parts_from_message(payload)
                    if text or raw_parts:
                        artifact_parts = []
                        if text:
                            artifact_parts.append({"kind": "text", "text": text})
                        artifact_parts.extend(raw_parts)
                        yield DispatchEvent(
                            type="artifact_update",
                            agent_message_id=agent_message_id,
                            data={
                                "raw": payload,
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

    # ──── JSON-RPC construction ────

    @staticmethod
    def _build_jsonrpc(
        message_dict: dict,
        base_method: str,
        version: str,
        configuration: dict[str, Any] | None = None,
    ) -> dict:
        """Build a JSON-RPC 2.0 envelope for an A2A message."""
        method = a2a_compat.get_method_name(base_method, version)
        params = a2a_compat.build_request_params(message_dict, version, configuration)
        return {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": params,
        }

    @staticmethod
    def _check_response(resp: httpx.Response) -> dict:
        """Parse response JSON and check for JSON-RPC errors.

        Raises A2AVersionFallbackError for fallback-eligible error codes.
        Raises RuntimeError for other JSON-RPC errors or unparseable 2xx bodies.
        Falls through to raise_for_status() for non-JSON non-2xx.
        """
        try:
            raw = resp.json()
        except (json.JSONDecodeError, ValueError):
            if resp.is_success:
                raise RuntimeError(
                    f"Agent returned HTTP {resp.status_code} with unparseable body"
                )
            resp.raise_for_status()
            return {}  # unreachable, raise_for_status always throws for non-2xx

        err = a2a_compat.extract_jsonrpc_error(raw)
        if err:
            if err.code in a2a_compat.FALLBACK_ELIGIBLE_CODES:
                raise A2AVersionFallbackError(
                    f"JSON-RPC error {err.code}: {err.message}"
                )
            raise RuntimeError(f"JSON-RPC error {err.code}: {err.message}")

        if not resp.is_success:
            resp.raise_for_status()

        return raw

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

    @classmethod
    def _extract_artifact_text(cls, inner: dict) -> str:
        """Extract text content from an artifact-update event."""
        text, _ = cls._collect_parts(inner.get("artifact", {}).get("parts", []))
        return text

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

    @classmethod
    def _extract_status_text(cls, inner: dict) -> str:
        """Extract text from a status-update event's message."""
        msg = inner.get("status", {}).get("message", {})
        text, _ = cls._collect_parts(msg.get("parts", []))
        return text

    @classmethod
    def _extract_message_text(cls, inner: dict) -> str:
        """Extract text from a message event."""
        text, _ = cls._collect_parts(inner.get("parts", []))
        return text

    @staticmethod
    def _collect_non_text_parts_from_message(inner: dict) -> list[dict]:
        """Collect non-text parts from a message event."""
        non_text: list[dict] = []
        for p in inner.get("parts", []):
            root = p.get("root", p)
            if "text" not in root:
                non_text.append(p)
        return non_text
