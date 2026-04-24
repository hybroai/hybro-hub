# hub/a2a_compat.py
"""A2A v0.3/v1.0 protocol compatibility layer.

Encapsulates all version-specific differences so that dispatcher.py
and agent_registry.py only call into this module for protocol decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedInterface:
    """A resolved JSON-RPC interface from an agent card."""

    binding: str
    protocol_version: str
    url: str


@dataclass(frozen=True)
class JsonRpcError:
    """A structured JSON-RPC error extracted from a response."""

    code: int
    message: str
    data: Any = None


class A2AVersionFallbackError(Exception):
    """Raised when a v1.0 dispatch gets a fallback-eligible JSON-RPC error."""


FALLBACK_ELIGIBLE_CODES: set[int] = {-32601, -32009}

CANONICAL_TERMINAL_STATES: set[str] = {
    "completed", "failed", "canceled", "rejected",
}


def validate_agent_card(card_data: dict) -> dict | None:
    """Validate an agent card, trying v0.3 (strict) first, then v1.0.

    Returns the card dict if valid, None if neither schema accepts it.
    v0.3 uses Pydantic model_validate which enforces required fields.
    v1.0 only accepts cards that have supportedInterfaces (the v1.0
    hallmark); protobuf ParseDict is too lenient on its own.
    """
    if not isinstance(card_data, dict) or not card_data.get("name"):
        return None

    # Try v0.3 (Pydantic — strict, enforces required fields)
    try:
        from a2a.compat.v0_3.types import AgentCard as V03AgentCard

        V03AgentCard.model_validate(card_data)
        return card_data
    except Exception:
        pass

    # Try v1.0 — require supportedInterfaces (v1.0's distinguishing key)
    ifaces = card_data.get("supportedInterfaces")
    if not isinstance(ifaces, list) or not ifaces:
        return None

    try:
        from google.protobuf.json_format import ParseDict
        from a2a.types import AgentCard

        parsed = ParseDict(card_data, AgentCard(), ignore_unknown_fields=True)
        if parsed.name:
            return card_data
    except Exception:
        pass

    return None


_SUPPORTED_VERSIONS: set[str] = {"0.3", "1.0"}


def select_interface(card: dict) -> ResolvedInterface:
    """Select the best JSON-RPC interface from an agent card.

    For v1.0 cards with supportedInterfaces: filter to JSONRPC, prefer v1.0.
    Only accepts versions in _SUPPORTED_VERSIONS (0.3, 1.0); interfaces
    advertising unsupported versions (e.g. 1.1, 2.0) are skipped.
    For v0.3 cards (no supportedInterfaces at all): use top-level url as v0.3.
    Raises ValueError if no usable JSON-RPC interface found.

    Important: the top-level url fallback is ONLY used when supportedInterfaces
    is absent. If supportedInterfaces is present but contains no supported
    versions (e.g. all 2.0), this raises ValueError rather than silently
    degrading to v0.3 via top-level url.
    """
    interfaces = card.get("supportedInterfaces", None)

    if interfaces is not None:
        if not isinstance(interfaces, list):
            raise ValueError(
                f"supportedInterfaces must be a list, got {type(interfaces).__name__}"
            )
        jsonrpc: list[ResolvedInterface] = []
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            if iface.get("protocolBinding", "") != "JSONRPC":
                continue
            url = iface.get("url", "")
            if not url:
                continue
            pv = iface.get("protocolVersion", "0.3")
            if pv not in _SUPPORTED_VERSIONS:
                continue
            jsonrpc.append(ResolvedInterface(binding="JSONRPC", protocol_version=pv, url=url))

        if jsonrpc:
            for target in ("1.0", "0.3"):
                for ri in jsonrpc:
                    if ri.protocol_version == target:
                        return ri
            return jsonrpc[0]

        raise ValueError(
            "supportedInterfaces present but no usable JSON-RPC interface "
            f"(supported versions: {_SUPPORTED_VERSIONS})"
        )

    url = card.get("url", "")
    if url:
        return ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=url)

    raise ValueError("No usable JSON-RPC interface in agent card")


def select_fallback_interface(
    card: dict, primary: ResolvedInterface,
) -> ResolvedInterface | None:
    """Select a v0.3 fallback interface for retry after v1.0 failure.

    Returns None if primary is already v0.3 (no fallback needed).
    For dual-mode cards: returns the v0.3 JSONRPC URL from the card.
    For single-interface v1.0 cards: synthesizes fallback at the same URL.
    """
    if primary.protocol_version == "0.3":
        return None

    for iface in card.get("supportedInterfaces", []):
        if not isinstance(iface, dict):
            continue
        if iface.get("protocolBinding", "") != "JSONRPC":
            continue
        pv = iface.get("protocolVersion", "0.3")
        url = iface.get("url", "")
        if pv == "0.3" and url:
            return ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=url)

    return ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=primary.url)


_METHOD_MAP_V03: dict[str, str] = {
    "send": "message/send",
    "stream": "message/stream",
    "get_task": "tasks/get",
    "cancel_task": "tasks/cancel",
}

_METHOD_MAP_V10: dict[str, str] = {
    "send": "SendMessage",
    "stream": "SendStreamingMessage",
    "get_task": "GetTask",
    "cancel_task": "CancelTask",
}

_V10_STATE_MAP: dict[str, str] = {
    "TASK_STATE_SUBMITTED": "submitted",
    "TASK_STATE_WORKING": "working",
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_REJECTED": "rejected",
    "TASK_STATE_INPUT_REQUIRED": "input-required",
    "TASK_STATE_AUTH_REQUIRED": "auth-required",
}

_V10_ROLE_MAP: dict[str, str] = {
    "ROLE_USER": "user",
    "ROLE_AGENT": "agent",
}

_V10_ROLE_ENCODE_MAP: dict[str, str] = {
    "user": "ROLE_USER",
    "agent": "ROLE_AGENT",
}


def get_method_name(base_method: str, version: str) -> str:
    """Map a canonical method name to the versioned JSON-RPC method string."""
    if version == "1.0":
        return _METHOD_MAP_V10[base_method]
    if version == "0.3":
        return _METHOD_MAP_V03[base_method]
    raise ValueError(f"Unsupported protocol version: {version}")


def get_headers(version: str) -> dict[str, str]:
    """Return extra HTTP headers for the given protocol version."""
    if version == "1.0":
        return {"A2A-Version": "1.0"}
    return {}


def normalize_task_state(state: str) -> str:
    """Normalize a task state to canonical lowercase."""
    return _V10_STATE_MAP.get(state, state)


def normalize_role(role: str) -> str:
    """Normalize a role to canonical lowercase."""
    return _V10_ROLE_MAP.get(role, role)


def build_role(role: str, version: str) -> str:
    """Encode a canonical lowercase role for the target protocol version."""
    canonical = normalize_role(role)
    if version == "1.0":
        return _V10_ROLE_ENCODE_MAP.get(canonical, role)
    return canonical


def build_configuration(
    configuration: dict[str, Any],
    version: str,
) -> dict[str, Any]:
    """Encode request configuration for the target protocol version."""
    if version != "1.0":
        return dict(configuration)

    cfg = dict(configuration)
    if "blocking" in cfg and "returnImmediately" not in cfg:
        cfg["returnImmediately"] = not bool(cfg.pop("blocking"))
    return cfg


def build_message_parts(parts: list[dict], version: str) -> list[dict]:
    """Convert canonical (flattened) parts to the target version's wire format.

    Canonical format matches v1.0: {"text": "..."}, {"url": "..."}, {"data": {...}}.
    For v0.3: adds `kind` discriminator, nests file fields under `file` key,
    renames mediaType->mimeType, filename->name, url->uri, raw->bytes.
    """
    if version != "0.3":
        return parts

    result: list[dict] = []
    for p in parts:
        if "text" in p:
            out: dict[str, Any] = {"kind": "text", "text": p["text"]}
            if "metadata" in p:
                out["metadata"] = p["metadata"]
            result.append(out)
        elif "url" in p:
            file_dict: dict[str, Any] = {"uri": p["url"]}
            if "mediaType" in p:
                file_dict["mimeType"] = p["mediaType"]
            if "filename" in p:
                file_dict["name"] = p["filename"]
            result.append({"kind": "file", "file": file_dict})
        elif "raw" in p:
            file_dict = {"bytes": p["raw"]}
            if "mediaType" in p:
                file_dict["mimeType"] = p["mediaType"]
            if "filename" in p:
                file_dict["name"] = p["filename"]
            result.append({"kind": "file", "file": file_dict})
        elif "data" in p:
            out = {"kind": "data", "data": p["data"]}
            if "metadata" in p:
                out["metadata"] = p["metadata"]
            result.append(out)
        else:
            result.append(p)
    return result


def normalize_inbound_parts(parts: list[dict], version: str) -> list[dict]:
    """Normalize inbound wire parts to canonical (flattened) format.

    For v0.3: strips `kind`, unnests file fields, renames mimeType->mediaType,
    name->filename, uri->url, bytes->raw.
    For v1.0: strips stale `kind` if present, renames mimeType->mediaType.
    """
    result: list[dict] = []
    for p in parts:
        kind = p.get("kind", "")
        if kind == "text":
            out: dict[str, Any] = {"text": p["text"]}
            if "metadata" in p:
                out["metadata"] = p["metadata"]
            result.append(out)
        elif kind == "file":
            f = p.get("file", {})
            out = {}
            if "uri" in f:
                out["url"] = f["uri"]
            if "bytes" in f:
                out["raw"] = f["bytes"]
            if "mimeType" in f:
                out["mediaType"] = f["mimeType"]
            if "name" in f:
                out["filename"] = f["name"]
            result.append(out)
        elif kind == "data":
            out = {"data": p["data"]}
            if "metadata" in p:
                out["metadata"] = p["metadata"]
            result.append(out)
        elif kind:
            result.append(p)
        else:
            out = dict(p)
            if "kind" in out:
                del out["kind"]
            if "mimeType" in out:
                out["mediaType"] = out.pop("mimeType")
            result.append(out)
    return result


def build_request_params(
    message_dict: dict,
    version: str,
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build JSON-RPC params for a message send/stream request."""
    msg = dict(message_dict)
    if version == "1.0":
        msg.pop("kind", None)
    if "role" in msg:
        msg["role"] = build_role(msg["role"], version)
    if "parts" in msg:
        canonical_parts = normalize_inbound_parts(msg["parts"], version)
        msg["parts"] = build_message_parts(canonical_parts, version)
    params: dict[str, Any] = {"message": msg}
    if configuration:
        params["configuration"] = build_configuration(configuration, version)
    return params


def extract_response(raw: dict, version: str) -> dict:
    """Normalize a JSON-RPC response to canonical format.

    Both v0.3 and v1.0 responses are normalized:
    - Parts in all messages/artifacts are converted to canonical (flattened) form.
    - v0.3: strips kind, unnests file fields, renames mimeType/name/uri/bytes.
    - v1.0: unwraps oneof wrapper (task/message), adds `kind` field,
      normalizes SCREAMING_SNAKE states/roles, normalizes parts.
    """
    if version != "1.0":
        _normalize_parts_in_result(raw.get("result", raw), version)
        return raw

    inner = raw.get("result", raw)

    if "task" in inner and isinstance(inner["task"], dict):
        task = inner["task"]
        task["kind"] = "task"
        _normalize_v10_task(task, version)
        return {"result": task}

    if "message" in inner and isinstance(inner["message"], dict) and "parts" in inner["message"]:
        msg = inner["message"]
        msg["kind"] = "message"
        if "role" in msg:
            msg["role"] = normalize_role(msg["role"])
        if "parts" in msg:
            msg["parts"] = normalize_inbound_parts(msg["parts"], version)
        return {"result": msg}

    if "status" in inner:
        _normalize_v10_task(inner, version)

    return raw


def _normalize_v10_task(task: dict, version: str) -> None:
    """Normalize task fields (states, roles, parts) to canonical form in place."""
    status = task.get("status", {})
    if "state" in status:
        status["state"] = normalize_task_state(status["state"])
    msg = status.get("message", {})
    if "role" in msg:
        msg["role"] = normalize_role(msg["role"])
    if "parts" in msg:
        msg["parts"] = normalize_inbound_parts(msg["parts"], version)
    for artifact in task.get("artifacts", []):
        if "parts" in artifact:
            artifact["parts"] = normalize_inbound_parts(artifact["parts"], version)


def _normalize_parts_in_result(inner: dict, version: str) -> None:
    """Normalize parts in a v0.3 result dict (task or message) in place."""
    if not isinstance(inner, dict):
        return
    if "parts" in inner:
        inner["parts"] = normalize_inbound_parts(inner["parts"], version)
    msg = inner.get("status", {}).get("message", {})
    if "parts" in msg:
        msg["parts"] = normalize_inbound_parts(msg["parts"], version)
    for artifact in inner.get("artifacts", []):
        if "parts" in artifact:
            artifact["parts"] = normalize_inbound_parts(artifact["parts"], version)


def classify_stream_event(
    data: dict, version: str,
) -> tuple[str, dict] | None:
    """Classify a streaming SSE event and normalize to canonical format.

    Returns (canonical_event_type, normalized_payload) or None if unrecognized.
    Both v0.3 and v1.0: parts in the payload are normalized to canonical flattened form.
    v0.3: uses `kind` discriminator.
    v1.0: uses ProtoJSON camelCase keys (statusUpdate, artifactUpdate, task, message).
    """
    if version == "1.0":
        return _classify_v10(data)

    kind = data.get("kind", "")
    if kind in ("status-update", "artifact-update", "task", "message"):
        _normalize_stream_event_parts(data, version)
        return (kind, data)
    return None


def _normalize_stream_event_parts(data: dict, version: str) -> None:
    """Normalize parts within a stream event payload in place."""
    if "parts" in data:
        data["parts"] = normalize_inbound_parts(data["parts"], version)
    artifact = data.get("artifact", {})
    if "parts" in artifact:
        artifact["parts"] = normalize_inbound_parts(artifact["parts"], version)
    msg = data.get("status", {}).get("message", {})
    if "parts" in msg:
        msg["parts"] = normalize_inbound_parts(msg["parts"], version)


def _classify_v10(data: dict) -> tuple[str, dict] | None:
    if "statusUpdate" in data:
        update = data["statusUpdate"]
        status = update.get("status", {})
        if "state" in status:
            status["state"] = normalize_task_state(status["state"])
        msg = status.get("message", {})
        if "role" in msg:
            msg["role"] = normalize_role(msg["role"])
        if "parts" in msg:
            msg["parts"] = normalize_inbound_parts(msg["parts"], "1.0")
        state = status.get("state", "")
        update["final"] = state in CANONICAL_TERMINAL_STATES
        update["status"] = status
        return ("status-update", update)

    if "artifactUpdate" in data:
        payload = data["artifactUpdate"]
        artifact = payload.get("artifact", {})
        if "parts" in artifact:
            artifact["parts"] = normalize_inbound_parts(artifact["parts"], "1.0")
        return ("artifact-update", payload)

    if "task" in data:
        task = data["task"]
        _normalize_v10_task(task, "1.0")
        return ("task", task)

    if "message" in data:
        msg = data["message"]
        if "role" in msg:
            msg["role"] = normalize_role(msg["role"])
        if "parts" in msg:
            msg["parts"] = normalize_inbound_parts(msg["parts"], "1.0")
        return ("message", msg)

    return None


def extract_jsonrpc_error(raw: dict) -> JsonRpcError | None:
    """Extract a JSON-RPC error from a response dict, or None if no error."""
    err = raw.get("error")
    if not err or not isinstance(err, dict):
        return None
    return JsonRpcError(
        code=err.get("code", 0),
        message=err.get("message", ""),
        data=err.get("data"),
    )
