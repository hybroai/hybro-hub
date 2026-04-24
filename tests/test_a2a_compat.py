# tests/test_a2a_compat.py
"""Tests for hub.a2a_compat — A2A v0.3/v1.0 protocol compat layer."""

import pytest

from hub.a2a_compat import (
    A2AVersionFallbackError,
    FALLBACK_ELIGIBLE_CODES,
    JsonRpcError,
    ResolvedInterface,
    build_message_parts,
    build_request_params,
    classify_stream_event,
    extract_jsonrpc_error,
    extract_response,
    get_headers,
    get_method_name,
    normalize_inbound_parts,
    normalize_role,
    normalize_task_state,
    select_fallback_interface,
    select_interface,
    validate_agent_card,
)


# ── Shared card fixtures ──

V03_CARD = {
    "name": "Legacy Agent",
    "description": "A legacy v0.3 agent",
    "url": "http://localhost:9001/",
    "version": "1.0.0",
    "capabilities": {"streaming": True},
    "skills": [{"id": "s1", "name": "Skill", "description": "A skill", "tags": ["chat"]}],
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
}

V10_CARD = {
    "name": "Modern Agent",
    "description": "A v1.0 agent",
    "supportedInterfaces": [
        {
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "url": "http://localhost:9002/a2a",
        },
    ],
    "capabilities": {"streaming": True},
    "skills": [{"id": "s1", "name": "Skill", "description": "A skill"}],
}

DUAL_MODE_CARD = {
    "name": "Dual Agent",
    "description": "Speaks both",
    "url": "http://localhost:9003/",
    "supportedInterfaces": [
        {
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "url": "http://localhost:9003/v1",
        },
        {
            "protocolBinding": "JSONRPC",
            "protocolVersion": "0.3",
            "url": "http://localhost:9003/v03",
        },
    ],
    "capabilities": {},
    "skills": [],
}


# ============================================================================
# Task 1: Core Data Types
# ============================================================================

class TestResolvedInterface:
    def test_frozen(self):
        ri = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001")
        assert ri.binding == "JSONRPC"
        assert ri.protocol_version == "1.0"
        assert ri.url == "http://localhost:9001"

    def test_equality(self):
        a = ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001")
        b = ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001")
        assert a == b


class TestJsonRpcError:
    def test_fields(self):
        err = JsonRpcError(code=-32601, message="Method not found")
        assert err.code == -32601
        assert err.message == "Method not found"
        assert err.data is None

    def test_with_data(self):
        err = JsonRpcError(code=-32009, message="Version not supported", data={"version": "1.0"})
        assert err.data == {"version": "1.0"}


class TestConstants:
    def test_fallback_eligible_codes(self):
        assert -32601 in FALLBACK_ELIGIBLE_CODES
        assert -32009 in FALLBACK_ELIGIBLE_CODES

    def test_fallback_error_is_exception(self):
        err = A2AVersionFallbackError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"


# ============================================================================
# Task 2: Card Validation
# ============================================================================

class TestValidateAgentCard:
    def test_v03_card_accepted(self):
        result = validate_agent_card(V03_CARD)
        assert result is not None
        assert result["name"] == "Legacy Agent"

    def test_v10_card_accepted(self):
        result = validate_agent_card(V10_CARD)
        assert result is not None
        assert result["name"] == "Modern Agent"

    def test_dual_mode_card_accepted(self):
        result = validate_agent_card(DUAL_MODE_CARD)
        assert result is not None

    def test_invalid_card_returns_none(self):
        result = validate_agent_card({"error": "not an agent"})
        assert result is None

    def test_empty_dict_returns_none(self):
        result = validate_agent_card({})
        assert result is None

    def test_card_with_vendor_extensions_accepted(self):
        card = {**V10_CARD, "x-vendor-feature": {"enabled": True}}
        result = validate_agent_card(card)
        assert result is not None

    def test_name_only_dict_rejected(self):
        """A dict with name but no A2A structural keys must be rejected."""
        result = validate_agent_card({"name": "not-a-card"})
        assert result is None

    def test_name_plus_unrelated_keys_rejected(self):
        result = validate_agent_card({"name": "foo", "error": "bar", "version": "1.0"})
        assert result is None

    def test_name_plus_empty_capabilities_rejected(self):
        result = validate_agent_card({"name": "x", "capabilities": {}})
        assert result is None

    def test_name_plus_url_only_rejected(self):
        result = validate_agent_card({"name": "x", "url": "http://localhost:1"})
        assert result is None

    def test_name_plus_single_shallow_key_rejected(self):
        result = validate_agent_card({"name": "x", "skills": []})
        assert result is None

    def test_fake_card_missing_required_v03_fields_rejected(self):
        result = validate_agent_card({
            "name": "x", "description": "y", "version": "1.0",
            "capabilities": {"streaming": True}, "url": "http://localhost:1",
        })
        assert result is None

    def test_sparse_v03_card_accepted(self):
        sparse = {
            "name": "Sparse", "description": "", "version": "1.0.0",
            "url": "http://localhost:9001/",
            "capabilities": {}, "skills": [],
            "defaultInputModes": [], "defaultOutputModes": [],
        }
        result = validate_agent_card(sparse)
        assert result is not None


# ============================================================================
# Task 3: Interface Selection
# ============================================================================

class TestSelectInterface:
    def test_v03_card_uses_top_level_url(self):
        ri = select_interface(V03_CARD)
        assert ri.binding == "JSONRPC"
        assert ri.protocol_version == "0.3"
        assert ri.url == "http://localhost:9001/"

    def test_v10_card_selects_jsonrpc_interface(self):
        ri = select_interface(V10_CARD)
        assert ri.binding == "JSONRPC"
        assert ri.protocol_version == "1.0"
        assert ri.url == "http://localhost:9002/a2a"

    def test_dual_mode_card_prefers_v10(self):
        ri = select_interface(DUAL_MODE_CARD)
        assert ri.protocol_version == "1.0"
        assert ri.url == "http://localhost:9003/v1"

    def test_card_with_only_v03_interface(self):
        card = {
            "name": "Old-style",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "protocolVersion": "0.3", "url": "http://localhost:9004/"},
            ],
        }
        ri = select_interface(card)
        assert ri.protocol_version == "0.3"

    def test_missing_version_defaults_to_v03(self):
        card = {
            "name": "No-version",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "url": "http://localhost:9005/"},
            ],
        }
        ri = select_interface(card)
        assert ri.protocol_version == "0.3"

    def test_skips_non_jsonrpc_bindings(self):
        card = {
            "name": "gRPC Agent",
            "supportedInterfaces": [
                {"protocolBinding": "gRPC", "protocolVersion": "1.0", "url": "grpc://localhost:50051"},
                {"protocolBinding": "JSONRPC", "protocolVersion": "1.0", "url": "http://localhost:9006/"},
            ],
        }
        ri = select_interface(card)
        assert ri.binding == "JSONRPC"
        assert ri.url == "http://localhost:9006/"

    def test_rejects_unsupported_version(self):
        card = {
            "name": "Future Agent",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "protocolVersion": "2.0", "url": "http://localhost:9007/"},
            ],
        }
        with pytest.raises(ValueError, match="supportedInterfaces present but no usable"):
            select_interface(card)

    def test_rejects_future_versions_even_with_top_level_url(self):
        """Card with supportedInterfaces (all future) + top-level url must NOT silently degrade to v0.3."""
        card = {
            "name": "Future Agent With URL",
            "url": "http://localhost:9007/",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "protocolVersion": "2.0", "url": "http://localhost:9007/v2"},
            ],
        }
        with pytest.raises(ValueError, match="supportedInterfaces present but no usable"):
            select_interface(card)

    def test_skips_unsupported_version_prefers_supported(self):
        card = {
            "name": "Mixed Future Agent",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "protocolVersion": "2.0", "url": "http://localhost:9007/v2"},
                {"protocolBinding": "JSONRPC", "protocolVersion": "1.0", "url": "http://localhost:9007/v1"},
            ],
        }
        ri = select_interface(card)
        assert ri.protocol_version == "1.0"
        assert ri.url == "http://localhost:9007/v1"

    def test_raises_when_no_usable_interface(self):
        card = {"name": "Useless", "supportedInterfaces": [
            {"protocolBinding": "gRPC", "url": "grpc://localhost:50051"},
        ]}
        with pytest.raises(ValueError, match="supportedInterfaces present but no usable"):
            select_interface(card)

    def test_raises_when_no_url(self):
        card = {"name": "Empty"}
        with pytest.raises(ValueError, match="No usable JSON-RPC interface"):
            select_interface(card)

    def test_raises_on_non_list_supported_interfaces(self):
        card = {"name": "Bad", "supportedInterfaces": {"oops": 1}}
        with pytest.raises(ValueError, match="must be a list"):
            select_interface(card)

    def test_skips_non_dict_items_in_supported_interfaces(self):
        card = {
            "name": "Mixed",
            "supportedInterfaces": [
                "not-a-dict",
                42,
                {"protocolBinding": "JSONRPC", "protocolVersion": "0.3", "url": "http://localhost:1/a2a"},
            ],
        }
        ri = select_interface(card)
        assert ri.protocol_version == "0.3"
        assert ri.url == "http://localhost:1/a2a"


class TestSelectFallbackInterface:
    def test_v03_primary_returns_none(self):
        primary = ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001/")
        assert select_fallback_interface(V03_CARD, primary) is None

    def test_dual_mode_returns_v03_url(self):
        primary = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9003/v1")
        fb = select_fallback_interface(DUAL_MODE_CARD, primary)
        assert fb is not None
        assert fb.protocol_version == "0.3"
        assert fb.url == "http://localhost:9003/v03"

    def test_single_v10_synthesizes_fallback_at_same_url(self):
        primary = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9002/a2a")
        fb = select_fallback_interface(V10_CARD, primary)
        assert fb is not None
        assert fb.protocol_version == "0.3"
        assert fb.url == "http://localhost:9002/a2a"

    def test_skips_non_dict_items_in_supported_interfaces(self):
        card = {
            "name": "Dirty",
            "supportedInterfaces": [
                "bad",
                {"protocolBinding": "JSONRPC", "protocolVersion": "0.3", "url": "http://localhost:1/v03"},
                {"protocolBinding": "JSONRPC", "protocolVersion": "1.0", "url": "http://localhost:1/v1"},
            ],
        }
        primary = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:1/v1")
        fb = select_fallback_interface(card, primary)
        assert fb is not None
        assert fb.protocol_version == "0.3"
        assert fb.url == "http://localhost:1/v03"


# ============================================================================
# Task 4: Wire Format Mapping
# ============================================================================

class TestGetMethodName:
    def test_v03_methods(self):
        assert get_method_name("send", "0.3") == "message/send"
        assert get_method_name("stream", "0.3") == "message/stream"
        assert get_method_name("get_task", "0.3") == "tasks/get"
        assert get_method_name("cancel_task", "0.3") == "tasks/cancel"

    def test_v10_methods(self):
        assert get_method_name("send", "1.0") == "SendMessage"
        assert get_method_name("stream", "1.0") == "SendStreamingMessage"
        assert get_method_name("get_task", "1.0") == "GetTask"
        assert get_method_name("cancel_task", "1.0") == "CancelTask"

    def test_unknown_method_raises(self):
        with pytest.raises(KeyError):
            get_method_name("unknown", "1.0")

    def test_unsupported_version_raises(self):
        with pytest.raises(ValueError, match="Unsupported protocol version"):
            get_method_name("send", "2.0")


class TestGetHeaders:
    def test_v10_includes_version_header(self):
        h = get_headers("1.0")
        assert h == {"A2A-Version": "1.0"}

    def test_v03_returns_empty(self):
        assert get_headers("0.3") == {}


class TestNormalizeTaskState:
    def test_v10_screaming_snake_to_lowercase(self):
        assert normalize_task_state("TASK_STATE_COMPLETED") == "completed"
        assert normalize_task_state("TASK_STATE_FAILED") == "failed"
        assert normalize_task_state("TASK_STATE_CANCELED") == "canceled"
        assert normalize_task_state("TASK_STATE_SUBMITTED") == "submitted"
        assert normalize_task_state("TASK_STATE_WORKING") == "working"
        assert normalize_task_state("TASK_STATE_REJECTED") == "rejected"
        assert normalize_task_state("TASK_STATE_INPUT_REQUIRED") == "input-required"
        assert normalize_task_state("TASK_STATE_AUTH_REQUIRED") == "auth-required"

    def test_already_lowercase_passes_through(self):
        assert normalize_task_state("completed") == "completed"
        assert normalize_task_state("working") == "working"

    def test_unknown_state_passes_through(self):
        assert normalize_task_state("some-future-state") == "some-future-state"


class TestNormalizeRole:
    def test_v10_screaming_to_lowercase(self):
        assert normalize_role("ROLE_USER") == "user"
        assert normalize_role("ROLE_AGENT") == "agent"

    def test_already_lowercase_passes_through(self):
        assert normalize_role("user") == "user"
        assert normalize_role("agent") == "agent"


# ============================================================================
# Task 5: Part Conversion
# ============================================================================

class TestBuildMessageParts:
    """Canonical (flattened) -> versioned wire format (outbound)."""

    def test_v10_text_passes_through(self):
        parts = [{"text": "hello"}]
        assert build_message_parts(parts, "1.0") == [{"text": "hello"}]

    def test_v10_file_url_passes_through(self):
        parts = [{"url": "https://example.com/f.pdf", "mediaType": "application/pdf", "filename": "f.pdf"}]
        assert build_message_parts(parts, "1.0") == parts

    def test_v10_data_passes_through(self):
        parts = [{"data": {"key": "val"}}]
        assert build_message_parts(parts, "1.0") == parts

    def test_v03_text_adds_kind(self):
        parts = [{"text": "hello"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "text", "text": "hello"}]

    def test_v03_text_preserves_metadata(self):
        parts = [{"text": "hello", "metadata": {"lang": "en"}}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "text", "text": "hello", "metadata": {"lang": "en"}}]

    def test_v03_file_by_url(self):
        parts = [{"url": "https://example.com/doc.pdf", "mediaType": "application/pdf", "filename": "doc.pdf"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{
            "kind": "file",
            "file": {"uri": "https://example.com/doc.pdf", "mimeType": "application/pdf", "name": "doc.pdf"},
        }]

    def test_v03_file_by_bytes(self):
        parts = [{"raw": "dGVzdA==", "mediaType": "image/png", "filename": "img.png"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{
            "kind": "file",
            "file": {"bytes": "dGVzdA==", "mimeType": "image/png", "name": "img.png"},
        }]

    def test_v03_data_part(self):
        parts = [{"data": {"key": "val"}}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "data", "data": {"key": "val"}}]

    def test_v03_data_preserves_metadata(self):
        parts = [{"data": {"key": "val"}, "metadata": {"source": "db"}}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "data", "data": {"key": "val"}, "metadata": {"source": "db"}}]

    def test_v03_mixed_parts(self):
        parts = [
            {"text": "See attached:"},
            {"url": "https://example.com/file.pdf", "mediaType": "application/pdf"},
        ]
        result = build_message_parts(parts, "0.3")
        assert len(result) == 2
        assert result[0] == {"kind": "text", "text": "See attached:"}
        assert result[1]["kind"] == "file"
        assert result[1]["file"]["uri"] == "https://example.com/file.pdf"

    def test_v03_unknown_part_passes_through(self):
        parts = [{"custom_field": "val"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"custom_field": "val"}]

    def test_empty_parts(self):
        assert build_message_parts([], "0.3") == []
        assert build_message_parts([], "1.0") == []


class TestNormalizeInboundParts:
    """Inbound wire parts -> canonical (flattened) format."""

    def test_v03_text_strips_kind(self):
        parts = [{"kind": "text", "text": "hello"}]
        assert normalize_inbound_parts(parts, "0.3") == [{"text": "hello"}]

    def test_v03_file_by_uri_unnested(self):
        parts = [{"kind": "file", "file": {"uri": "https://example.com/doc.pdf", "mimeType": "application/pdf", "name": "doc.pdf"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"url": "https://example.com/doc.pdf", "mediaType": "application/pdf", "filename": "doc.pdf"}]

    def test_v03_file_by_bytes_unnested(self):
        parts = [{"kind": "file", "file": {"bytes": "dGVzdA==", "mimeType": "image/png", "name": "img.png"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"raw": "dGVzdA==", "mediaType": "image/png", "filename": "img.png"}]

    def test_v03_data_strips_kind(self):
        parts = [{"kind": "data", "data": {"key": "val"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"data": {"key": "val"}}]

    def test_v03_data_preserves_metadata(self):
        parts = [{"kind": "data", "data": {"key": "val"}, "metadata": {"source": "db"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"data": {"key": "val"}, "metadata": {"source": "db"}}]

    def test_v03_text_preserves_metadata(self):
        parts = [{"kind": "text", "text": "hi", "metadata": {"x": 1}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"text": "hi", "metadata": {"x": 1}}]

    def test_v03_already_flattened_passes_through(self):
        parts = [{"text": "already flat"}]
        assert normalize_inbound_parts(parts, "0.3") == [{"text": "already flat"}]

    def test_v03_unknown_part_passes_through(self):
        parts = [{"custom": "field"}]
        assert normalize_inbound_parts(parts, "0.3") == [{"custom": "field"}]

    def test_v10_passes_through(self):
        parts = [{"text": "hello"}, {"url": "https://example.com/f.pdf"}]
        assert normalize_inbound_parts(parts, "1.0") == parts

    def test_v10_strips_stale_kind(self):
        parts = [{"kind": "text", "text": "hello"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"text": "hello"}]

    def test_v10_renames_mimeType_to_mediaType(self):
        parts = [{"url": "https://example.com/f.pdf", "mimeType": "application/pdf"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"url": "https://example.com/f.pdf", "mediaType": "application/pdf"}]

    def test_roundtrip_v03_text(self):
        canonical = [{"text": "hello"}]
        wire = build_message_parts(canonical, "0.3")
        back = normalize_inbound_parts(wire, "0.3")
        assert back == canonical

    def test_roundtrip_v03_file_by_url(self):
        canonical = [{"url": "https://example.com/doc.pdf", "mediaType": "application/pdf", "filename": "doc.pdf"}]
        wire = build_message_parts(canonical, "0.3")
        back = normalize_inbound_parts(wire, "0.3")
        assert back == canonical

    def test_roundtrip_v03_file_by_bytes(self):
        canonical = [{"raw": "dGVzdA==", "mediaType": "image/png", "filename": "img.png"}]
        wire = build_message_parts(canonical, "0.3")
        back = normalize_inbound_parts(wire, "0.3")
        assert back == canonical

    def test_roundtrip_v03_data(self):
        canonical = [{"data": {"key": "val"}}]
        wire = build_message_parts(canonical, "0.3")
        back = normalize_inbound_parts(wire, "0.3")
        assert back == canonical

    def test_roundtrip_v10_text(self):
        canonical = [{"text": "hello"}]
        wire = build_message_parts(canonical, "1.0")
        back = normalize_inbound_parts(wire, "1.0")
        assert back == canonical

    def test_empty_parts(self):
        assert normalize_inbound_parts([], "0.3") == []
        assert normalize_inbound_parts([], "1.0") == []


# ============================================================================
# Task 6: Request Params
# ============================================================================

class TestBuildRequestParams:
    def test_v10_passes_parts_through(self):
        msg = {"role": "user", "parts": [{"text": "hi"}], "messageId": "m1"}
        params = build_request_params(msg, "1.0")
        assert params["message"]["parts"] == [{"text": "hi"}]
        assert "configuration" not in params

    def test_v10_strips_top_level_kind_from_message(self):
        msg = {
            "kind": "message",
            "role": "user",
            "parts": [{"text": "hi"}],
            "messageId": "m1",
        }
        params = build_request_params(msg, "1.0")
        assert "kind" not in params["message"]

    def test_v10_strips_v03_kind_from_parts(self):
        msg = {
            "role": "user",
            "parts": [{"kind": "text", "text": "hi"}],
            "messageId": "m1",
        }
        params = build_request_params(msg, "1.0")
        assert params["message"]["parts"] == [{"text": "hi"}]

    def test_v10_encodes_role_enum(self):
        msg = {"role": "user", "parts": [{"text": "hi"}], "messageId": "m1"}
        params = build_request_params(msg, "1.0")
        assert params["message"]["role"] == "ROLE_USER"

    def test_v03_adds_kind_to_parts(self):
        msg = {"role": "user", "parts": [{"text": "hi"}], "messageId": "m1"}
        params = build_request_params(msg, "0.3")
        assert params["message"]["parts"] == [{"kind": "text", "text": "hi"}]

    def test_with_configuration(self):
        msg = {"role": "user", "parts": [{"text": "hi"}]}
        params = build_request_params(msg, "1.0", configuration={"blocking": True})
        assert params["configuration"] == {"returnImmediately": False}

    def test_does_not_mutate_original(self):
        msg = {"role": "user", "parts": [{"text": "hi"}]}
        original_parts = msg["parts"]
        build_request_params(msg, "0.3")
        assert msg["parts"] is original_parts
        assert "kind" not in msg["parts"][0]


# ============================================================================
# Task 7: Response Extraction
# ============================================================================

class TestExtractResponse:
    """extract_response normalizes responses to canonical format compatible
    with the existing _extract_response_content dispatcher method.
    Both v0.3 and v1.0 responses have parts normalized to canonical (flattened)."""

    def test_v03_normalizes_parts(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "task",
                "id": "t-1",
                "status": {
                    "state": "completed",
                    "message": {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": "done"}],
                    },
                },
            },
        }
        result = extract_response(raw, "0.3")
        assert result["result"]["status"]["message"]["parts"] == [{"text": "done"}]

    def test_v03_normalizes_artifact_parts(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "task",
                "id": "t-1",
                "status": {"state": "completed"},
                "artifacts": [
                    {"parts": [{"kind": "file", "file": {"uri": "https://example.com/f.pdf", "mimeType": "application/pdf", "name": "doc.pdf"}}]},
                ],
            },
        }
        result = extract_response(raw, "0.3")
        assert result["result"]["artifacts"][0]["parts"] == [{"url": "https://example.com/f.pdf", "mediaType": "application/pdf", "filename": "doc.pdf"}]

    def test_v03_normalizes_message_kind_parts(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "message",
                "parts": [{"kind": "text", "text": "hello"}, {"kind": "data", "data": {"k": "v"}}],
            },
        }
        result = extract_response(raw, "0.3")
        assert result["result"]["parts"] == [{"text": "hello"}, {"data": {"k": "v"}}]

    def test_v10_task_unwrapped_and_normalized(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "done"}],
                        },
                    },
                },
            },
        }
        result = extract_response(raw, "1.0")
        inner = result["result"]
        assert inner["kind"] == "task"
        assert inner["id"] == "t-1"
        assert inner["status"]["state"] == "completed"
        assert inner["status"]["message"]["role"] == "agent"

    def test_v10_message_unwrapped_and_normalized(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "message": {
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "hi"}],
                    "messageId": "m-1",
                },
            },
        }
        result = extract_response(raw, "1.0")
        inner = result["result"]
        assert inner["kind"] == "message"
        assert inner["role"] == "agent"
        assert inner["parts"] == [{"text": "hi"}]

    def test_v10_direct_task_normalized(self):
        """GetTask response: result IS the task directly (no oneof wrapper)."""
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "id": "t-1",
                "status": {
                    "state": "TASK_STATE_WORKING",
                },
            },
        }
        result = extract_response(raw, "1.0")
        assert result["result"]["status"]["state"] == "working"

    def test_v10_task_with_artifacts(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {"parts": [{"text": "artifact text"}]},
                    ],
                },
            },
        }
        result = extract_response(raw, "1.0")
        inner = result["result"]
        assert inner["kind"] == "task"
        assert inner["status"]["state"] == "completed"
        assert inner["artifacts"][0]["parts"] == [{"text": "artifact text"}]


# ============================================================================
# Task 8: Stream Event Classification
# ============================================================================

class TestClassifyStreamEvent:
    # ── v0.3 events ──

    def test_v03_status_update(self):
        data = {"kind": "status-update", "status": {"state": "working"}, "final": False}
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "status-update"
        assert payload["status"]["state"] == "working"

    def test_v03_artifact_update_normalizes_parts(self):
        data = {"kind": "artifact-update", "artifact": {"parts": [{"kind": "text", "text": "chunk"}]}}
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "artifact-update"
        assert payload["artifact"]["parts"] == [{"text": "chunk"}]

    def test_v03_task(self):
        data = {"kind": "task", "id": "t-1"}
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "task"
        assert payload["id"] == "t-1"

    def test_v03_message_normalizes_parts(self):
        data = {"kind": "message", "parts": [{"kind": "text", "text": "hi"}]}
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "message"
        assert payload["parts"] == [{"text": "hi"}]

    def test_v03_unknown_kind_returns_none(self):
        data = {"kind": "unknown-thing"}
        assert classify_stream_event(data, "0.3") is None

    # ── v1.0 events ──

    def test_v10_status_update(self):
        data = {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}}
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "status-update"
        assert payload["status"]["state"] == "working"

    def test_v10_status_update_terminal_sets_final(self):
        data = {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}
        _, payload = classify_stream_event(data, "1.0")
        assert payload["final"] is True

    def test_v10_status_update_nonterminal_sets_final_false(self):
        data = {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}}
        _, payload = classify_stream_event(data, "1.0")
        assert payload["final"] is False

    def test_v10_artifact_update(self):
        data = {"artifactUpdate": {"artifact": {"parts": [{"text": "chunk"}]}, "append": True}}
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "artifact-update"
        assert payload["artifact"]["parts"] == [{"text": "chunk"}]

    def test_v10_task(self):
        data = {"task": {"id": "t-1", "status": {"state": "TASK_STATE_SUBMITTED"}}}
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "task"
        assert payload["id"] == "t-1"
        assert payload["status"]["state"] == "submitted"

    def test_v10_message(self):
        data = {"message": {"role": "ROLE_AGENT", "parts": [{"text": "hi"}]}}
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "message"
        assert payload["role"] == "agent"

    def test_v10_unknown_returns_none(self):
        data = {"somethingNew": {"value": 1}}
        assert classify_stream_event(data, "1.0") is None

    def test_v10_status_normalizes_role_in_message(self):
        data = {
            "statusUpdate": {
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": "need input"}]},
                },
            },
        }
        _, payload = classify_stream_event(data, "1.0")
        assert payload["status"]["state"] == "input-required"
        assert payload["status"]["message"]["role"] == "agent"
        assert payload["final"] is False


# ============================================================================
# Task 9: JSON-RPC Error Extraction
# ============================================================================

class TestExtractJsonrpcError:
    def test_no_error_returns_none(self):
        raw = {"jsonrpc": "2.0", "id": "1", "result": {"kind": "task"}}
        assert extract_jsonrpc_error(raw) is None

    def test_extracts_method_not_found(self):
        raw = {"jsonrpc": "2.0", "id": "1", "error": {"code": -32601, "message": "Method not found"}}
        err = extract_jsonrpc_error(raw)
        assert err is not None
        assert err.code == -32601
        assert err.message == "Method not found"
        assert err.data is None

    def test_extracts_version_not_supported(self):
        raw = {"jsonrpc": "2.0", "id": "1", "error": {"code": -32009, "message": "Version not supported", "data": {"supported": ["0.3"]}}}
        err = extract_jsonrpc_error(raw)
        assert err is not None
        assert err.code == -32009
        assert err.data == {"supported": ["0.3"]}

    def test_non_dict_error_returns_none(self):
        raw = {"jsonrpc": "2.0", "id": "1", "error": "string error"}
        assert extract_jsonrpc_error(raw) is None

    def test_missing_code_defaults_to_zero(self):
        raw = {"jsonrpc": "2.0", "id": "1", "error": {"message": "oops"}}
        err = extract_jsonrpc_error(raw)
        assert err is not None
        assert err.code == 0

    def test_empty_dict_returns_none(self):
        assert extract_jsonrpc_error({}) is None

    def test_fallback_eligible_check(self):
        err_32601 = JsonRpcError(code=-32601, message="Method not found")
        err_32009 = JsonRpcError(code=-32009, message="Version not supported")
        err_other = JsonRpcError(code=-32600, message="Invalid Request")
        assert err_32601.code in FALLBACK_ELIGIBLE_CODES
        assert err_32009.code in FALLBACK_ELIGIBLE_CODES
        assert err_other.code not in FALLBACK_ELIGIBLE_CODES
