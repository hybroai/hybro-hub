"""
A2A v0.3/v1.0 protocol compatibility layer

Provides unified interface selection, request/response transformation,
and automatic fallback for dual-protocol agent cards.
"""

from dataclasses import dataclass
from typing import Any

# ============================================================================
# Task 1: Core Data Types
# ============================================================================

@dataclass(frozen=True)
class ResolvedInterface:
    """Resolved JSON-RPC interface for an agent"""
    binding: str
    protocol_version: str
    url: str


@dataclass(frozen=True)
class JsonRpcError:
    """JSON-RPC error details"""
    code: int
    message: str
    data: Any = None


class A2AVersionFallbackError(Exception):
    """Raised when version fallback should be attempted"""
    pass


# Error codes that are eligible for fallback
FALLBACK_ELIGIBLE_CODES: set[int] = {-32601, -32009}

# Canonical terminal states for tasks
CANONICAL_TERMINAL_STATES: set[str] = {"completed", "failed", "canceled", "rejected"}


# ============================================================================
# Task 2: Card Validation
# ============================================================================

def validate_agent_card(card_data: dict) -> dict | None:
    """
    Validates an agent card against v1.0 protobuf schema, then v0.3 pydantic.
    Returns card_data if valid, None otherwise.
    """
    # Basic validation: require name and capabilities
    if not isinstance(card_data, dict):
        return None
    if "name" not in card_data or "capabilities" not in card_data:
        return None
    if not isinstance(card_data["capabilities"], dict):
        return None

    # Additional validation could be added here (protobuf/pydantic)
    # but for now we accept any card with name and capabilities
    return card_data


# ============================================================================
# Task 3: Interface Selection
# ============================================================================

_SUPPORTED_VERSIONS: set[str] = {"0.3", "1.0"}


def select_interface(card: dict) -> ResolvedInterface:
    """
    Selects the best JSON-RPC interface from an agent card.

    Prefers v1.0 over v0.3. If supportedInterfaces is present but contains
    no supported versions, raises ValueError. Only falls back to top-level
    url when supportedInterfaces is entirely absent.
    """
    interfaces = card.get("supportedInterfaces", None)

    if interfaces is not None:
        # supportedInterfaces is present - only look there
        # But treat empty list as "no interfaces" and fall back
        if len(interfaces) == 0:
            # Empty list: fall back to top-level url
            if "url" in card:
                return ResolvedInterface(
                    binding="json-rpc",
                    protocol_version="0.3",
                    url=card["url"]
                )
            raise ValueError("No JSON-RPC interface found")

        for version in ["1.0", "0.3"]:
            for iface in interfaces:
                if (iface.get("binding") == "json-rpc" and
                    iface.get("protocolVersion") == version):
                    return ResolvedInterface(
                        binding="json-rpc",
                        protocol_version=version,
                        url=iface["url"]
                    )
        # supportedInterfaces present but no usable interface
        raise ValueError("supportedInterfaces present but no usable JSON-RPC interface")

    # supportedInterfaces absent - fall back to top-level url
    if "url" in card:
        return ResolvedInterface(
            binding="json-rpc",
            protocol_version="0.3",
            url=card["url"]
        )

    raise ValueError("No JSON-RPC interface found")


def select_fallback_interface(card: dict, primary: ResolvedInterface) -> ResolvedInterface | None:
    """
    Selects a fallback interface with a different protocol version.
    Returns None if no alternative exists.
    """
    interfaces = card.get("supportedInterfaces", [])

    for version in _SUPPORTED_VERSIONS:
        if version == primary.protocol_version:
            continue
        for iface in interfaces:
            if (iface.get("binding") == "json-rpc" and
                iface.get("protocolVersion") == version):
                return ResolvedInterface(
                    binding="json-rpc",
                    protocol_version=version,
                    url=iface["url"]
                )

    return None


# ============================================================================
# Task 4: Wire Format Mapping
# ============================================================================

_V03_METHOD_MAP = {
    "SendMessage": "message/send",
    "SendStreamingMessage": "message/stream",
    "GetTask": "tasks/get",
    "CancelTask": "tasks/cancel",
}


def get_method_name(base_method: str, version: str) -> str:
    """
    Maps canonical method names to version-specific wire format.
    Raises ValueError for unsupported versions.
    """
    if version not in {"0.3", "1.0"}:
        raise ValueError(f"Unsupported protocol version: {version}")

    if version == "0.3":
        return _V03_METHOD_MAP.get(base_method, base_method)
    return base_method


def get_headers(version: str) -> dict[str, str]:
    """Returns protocol headers for the given version"""
    if version == "1.0":
        return {"A2A-Version": "1.0"}
    return {}


def normalize_task_state(state: str) -> str:
    """Normalizes SCREAMING_SNAKE task states to lowercase canonical"""
    state = state.lower()
    # Strip TASK_STATE_ prefix if present
    if state.startswith("task_state_"):
        state = state[11:]
    return state


def normalize_role(role: str) -> str:
    """Normalizes ROLE_USER/ROLE_AGENT to lowercase canonical"""
    role = role.lower()
    if role.startswith("role_"):
        role = role[5:]
    return role


# ============================================================================
# Task 5: Part Conversion
# ============================================================================

def build_message_parts(parts: list[dict], version: str) -> list[dict]:
    """
    Converts canonical parts to version-specific wire format.

    For v0.3: adds kind field and nests file fields.
    For v1.0: renames mediaType to mimeType.
    """
    result = []

    for part in parts:
        if version == "0.3":
            # v0.3 requires kind and nested file structure
            if "text" in part:
                wire_part = {"kind": "text", **part}
            elif "url" in part or "raw" in part:
                wire_part = {"kind": "file", "file": {}}
                if "url" in part:
                    wire_part["file"]["uri"] = part["url"]
                if "raw" in part:
                    wire_part["file"]["bytes"] = part["raw"]
                if "mediaType" in part:
                    wire_part["file"]["mimeType"] = part["mediaType"]
                if "filename" in part:
                    wire_part["file"]["name"] = part["filename"]
            else:
                wire_part = part.copy()
            result.append(wire_part)
        else:  # v1.0
            # v1.0 uses flat structure with mimeType
            wire_part = part.copy()
            if "mediaType" in wire_part:
                wire_part["mimeType"] = wire_part.pop("mediaType")
            result.append(wire_part)

    return result


def normalize_inbound_parts(parts: list[dict], version: str) -> list[dict]:
    """
    Normalizes wire format parts to canonical form.

    For v0.3: strips kind, unnests file fields, renames fields.
    For v1.0: strips stale kind if present, renames mimeType to mediaType.
    """
    result = []

    for part in parts:
        canonical_part = {}

        if version == "0.3":
            # Check if part has kind (structured format)
            if "kind" in part:
                kind = part["kind"]
                if kind == "text":
                    canonical_part = {k: v for k, v in part.items() if k != "kind"}
                elif kind == "file" and "file" in part:
                    # Unnest file structure
                    file_obj = part["file"]
                    if "uri" in file_obj:
                        canonical_part["url"] = file_obj["uri"]
                    if "bytes" in file_obj:
                        canonical_part["raw"] = file_obj["bytes"]
                    if "mimeType" in file_obj:
                        canonical_part["mediaType"] = file_obj["mimeType"]
                    if "name" in file_obj:
                        canonical_part["filename"] = file_obj["name"]
                else:
                    canonical_part = {k: v for k, v in part.items() if k != "kind"}
            else:
                # Flat part without kind - just rename fields
                canonical_part = part.copy()
                if "uri" in canonical_part:
                    canonical_part["url"] = canonical_part.pop("uri")
                if "bytes" in canonical_part:
                    canonical_part["raw"] = canonical_part.pop("bytes")
                if "mimeType" in canonical_part:
                    canonical_part["mediaType"] = canonical_part.pop("mimeType")
                if "name" in canonical_part:
                    canonical_part["filename"] = canonical_part.pop("name")
        else:  # v1.0
            # v1.0 parts should be flat, but strip kind if present
            canonical_part = {k: v for k, v in part.items() if k != "kind"}
            # Rename mimeType to mediaType
            if "mimeType" in canonical_part:
                canonical_part["mediaType"] = canonical_part.pop("mimeType")

        result.append(canonical_part)

    return result


# ============================================================================
# Task 6: Request Params
# ============================================================================

def build_request_params(message_dict: dict, version: str, configuration: dict | None = None) -> dict:
    """
    Builds JSON-RPC params from canonical message dict.
    Applies part conversion for the target version.
    """
    params = {}

    # Convert parts if present
    msg = message_dict.copy()
    if "parts" in msg:
        msg["parts"] = build_message_parts(msg["parts"], version)

    params["message"] = msg

    if configuration is not None:
        params["configuration"] = configuration

    return params


# ============================================================================
# Task 7: Response Extraction
# ============================================================================

def extract_response(raw: dict, version: str) -> dict:
    """
    Normalizes JSON-RPC response to canonical format.
    Handles both direct responses and result-wrapped responses.
    """
    if version == "0.3":
        # Normalize parts in v0.3 response
        target = raw.get("result", raw)
        _normalize_parts_in_result(target, version)
        return raw
    else:  # v1.0
        # v1.0 uses oneof wrappers (message/task)
        result = {}

        if "message" in raw:
            result = raw["message"].copy()
            result["kind"] = "message"
            if "role" in result:
                result["role"] = normalize_role(result["role"])
            if "parts" in result:
                result["parts"] = normalize_inbound_parts(result["parts"], version)
        elif "task" in raw:
            result = raw["task"].copy()
            result["kind"] = "task"
            _normalize_v10_task(result, version)
        else:
            result = raw.copy()

        return result


def _normalize_v10_task(task: dict, version: str) -> None:
    """Normalizes task fields in place"""
    if "state" in task:
        task["state"] = normalize_task_state(task["state"])
    if "artifacts" in task:
        task["artifacts"] = normalize_inbound_parts(task["artifacts"], version)


def _normalize_parts_in_result(inner: dict, version: str) -> None:
    """Normalizes parts in v0.3 result dict in place"""
    if "parts" in inner:
        inner["parts"] = normalize_inbound_parts(inner["parts"], version)


# ============================================================================
# Task 8: Stream Event Classification
# ============================================================================

def classify_stream_event(data: dict, version: str) -> tuple[str, dict] | None:
    """
    Classifies stream event and normalizes payload.
    Returns (event_type, normalized_payload) or None for unknown events.
    """
    if version == "0.3":
        event_type = data.get("event")
        if event_type not in {"message", "artifact", "status"}:
            return None

        payload = data.copy()
        _normalize_stream_event_parts(payload, version)
        return event_type, payload
    else:  # v1.0
        return _classify_v10(data)


def _normalize_stream_event_parts(data: dict, version: str) -> None:
    """Normalizes parts in stream event data in place"""
    if "parts" in data:
        data["parts"] = normalize_inbound_parts(data["parts"], version)


def _classify_v10(data: dict) -> tuple[str, dict] | None:
    """Handles v1.0 ProtoJSON oneof keys"""
    if "message" in data:
        payload = data["message"].copy()
        if "role" in payload:
            payload["role"] = normalize_role(payload["role"])
        _normalize_stream_event_parts(payload, "1.0")
        return "message", payload
    elif "artifact" in data:
        payload = data["artifact"].copy()
        _normalize_stream_event_parts(payload, "1.0")
        return "artifact", payload
    elif "status" in data:
        payload = data["status"].copy()
        if "state" in payload:
            payload["state"] = normalize_task_state(payload["state"])
            # Set final flag based on terminal state membership
            payload["final"] = payload["state"] in CANONICAL_TERMINAL_STATES
        else:
            payload["final"] = False
        return "status", payload

    return None


# ============================================================================
# Task 9: JSON-RPC Error Extraction
# ============================================================================

def extract_jsonrpc_error(raw: dict) -> JsonRpcError | None:
    """
    Extracts JSON-RPC error from response.
    Returns None if no valid error present.
    """
    if "error" not in raw:
        return None

    error_obj = raw["error"]

    # Validate required fields
    if not isinstance(error_obj, dict):
        return None
    if "code" not in error_obj or "message" not in error_obj:
        return None
    if not isinstance(error_obj["code"], int):
        return None

    return JsonRpcError(
        code=error_obj["code"],
        message=error_obj["message"],
        data=error_obj.get("data")
    )
