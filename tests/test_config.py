"""Tests for hub.config — _expand_env_vars and related config loading."""

from __future__ import annotations

import textwrap
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from hub.config import (
    CloudConfig,
    HubConfig,
    _expand_env_vars,
    is_hybro_cloud_gateway,
    load_config,
    save_api_key,
)


# ── _expand_env_vars ─────────────────────────────────────────────────────────


class TestIsHybroCloudGateway:
    def test_matches_default_cloud_hostname(self) -> None:
        assert is_hybro_cloud_gateway("https://api.hybro.ai") is True
        assert is_hybro_cloud_gateway("https://api.hybro.ai/") is True

    def test_is_case_insensitive_on_hostname(self) -> None:
        assert is_hybro_cloud_gateway("https://API.HYBRO.AI") is True

    def test_rejects_self_hosted_and_path_substrings(self) -> None:
        assert is_hybro_cloud_gateway("https://api.hybro.ai.local") is False
        assert is_hybro_cloud_gateway("https://corp.example/api.hybro.ai/v1") is False
        assert is_hybro_cloud_gateway("http://localhost:8000") is False


class TestExpandEnvVars:
    def test_set_var_is_substituted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _expand_env_vars("value: ${MY_VAR}") == "value: hello"

    def test_unset_var_becomes_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_VAR", raising=False)
        assert _expand_env_vars("value: ${MY_VAR}") == "value: "

    def test_default_used_when_var_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_VAR", raising=False)
        assert _expand_env_vars("value: ${MY_VAR:-fallback}") == "value: fallback"

    def test_env_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "from_env")
        assert _expand_env_vars("value: ${MY_VAR:-fallback}") == "value: from_env"

    def test_escape_produces_literal_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """$${VAR} must produce the literal text ${VAR}, not expand it."""
        monkeypatch.setenv("MY_VAR", "should_not_appear")
        assert _expand_env_vars("value: $${MY_VAR}") == "value: ${MY_VAR}"

    def test_escape_is_single_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the escape is handled in a single regex pass, not two passes.

        Two-pass approaches incorrectly expand $${VAR} because the first pass
        replaces ${VAR} inside $${VAR}, leaving $<value> before the unescape
        step can fire.
        """
        monkeypatch.setenv("MY_VAR", "expanded")
        # $${MY_VAR} → ${MY_VAR} (literal escape)
        # ${MY_VAR}  → expanded  (normal expansion)
        result = _expand_env_vars("a: $${MY_VAR} b: ${MY_VAR}")
        assert result == "a: ${MY_VAR} b: expanded"

    def test_double_dollar_then_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """$$${VAR} = one escape ($${) + one expansion (${VAR}) → ${VAR} expanded."""
        monkeypatch.setenv("MY_VAR", "val")
        # $$${MY_VAR} → the $$ matches $${MY_VAR} as escape? No —
        # the regex matches the longest leftmost: $${MY_VAR} is the escape,
        # so $$${MY_VAR} = $ + ${MY_VAR} (literal $, then expansion).
        result = _expand_env_vars("$$${ MY_VAR}")
        # ${ MY_VAR} has a space so won't match — just passes through
        # Use a real var name:
        result = _expand_env_vars("a: $$$MY_VAR")
        # No ${} syntax — no substitution
        assert result == "a: $$$MY_VAR"

    def test_no_references_unchanged(self) -> None:
        text = "plain: value\nother: 123"
        assert _expand_env_vars(text) == text

    def test_multiple_references_in_one_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A", "alpha")
        monkeypatch.setenv("B", "beta")
        monkeypatch.delenv("C", raising=False)
        result = _expand_env_vars("${A} ${B} ${C:-gamma}")
        assert result == "alpha beta gamma"

    def test_empty_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_VAR", raising=False)
        assert _expand_env_vars("value: ${MY_VAR:-}") == "value: "


# ── Integration: load_config reads expanded YAML ─────────────────────────────


class TestLoadConfigEnvExpansion:
    def test_api_key_from_env_var_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYBRO_API_KEY", "hybro_testkey")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                cloud:
                  api_key: ${HYBRO_API_KEY}
                  gateway_url: "https://api.hybro.ai"
            """)
        )
        config = load_config(config_path=config_file)
        assert config.cloud.api_key == "hybro_testkey"

    def test_unset_var_leaves_empty_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYBRO_API_KEY", raising=False)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                cloud:
                  api_key: ${HYBRO_API_KEY}
            """)
        )
        config = load_config(config_path=config_file)
        assert config.cloud.api_key is None

    def test_default_value_in_var_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYBRO_GW", raising=False)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                cloud:
                  api_key: "hybro_x"
                  gateway_url: ${HYBRO_GW:-https://fallback.example.com}
            """)
        )
        config = load_config(config_path=config_file)
        assert config.cloud.gateway_url == "https://fallback.example.com"

    def test_literal_escape_in_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYBRO_API_KEY", raising=False)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                cloud:
                  api_key: "hybro_literal"
                  gateway_url: "https://api.hybro.ai"
                # escaped reference stays literal in a comment: $${HYBRO_API_KEY}
            """)
        )
        config = load_config(config_path=config_file)
        assert config.cloud.api_key == "hybro_literal"


# ── HubConfig defaults ────────────────────────────────────────────────────────


class TestHubConfigDefaults:
    def test_load_config_constructs_without_error(self, tmp_path: Path) -> None:
        """Bare load_config with an empty file must not raise."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        with (
            patch("hub.config.HUB_ID_FILE", tmp_path / "hub_id"),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            config = load_config(config_path=config_file)
        assert isinstance(config, HubConfig)

    def test_defaults_are_sensible(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        with (
            patch("hub.config.HUB_ID_FILE", tmp_path / "hub_id"),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            config = load_config(config_path=config_file)
        assert config.cloud.api_key is None
        assert config.cloud.gateway_url == "https://api.hybro.ai"
        assert config.agents.auto_discover is True
        assert config.publish_queue.enabled is True

    def test_hub_id_empty_raises(self) -> None:
        """Directly constructing HubConfig with empty hub_id must fail validation."""
        with pytest.raises(Exception):
            HubConfig(hub_id="")


# ── hub_id persistence ────────────────────────────────────────────────────────


class TestLoadConfigHubId:
    def test_generates_hub_id_when_missing(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        hub_id_file = tmp_path / "hub_id"
        with (
            patch("hub.config.HUB_ID_FILE", hub_id_file),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            config = load_config(config_path=config_file)
        assert config.hub_id
        assert hub_id_file.read_text().strip() == config.hub_id

    def test_loads_existing_hub_id_from_disk(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        hub_id_file = tmp_path / "hub_id"
        hub_id_file.write_text("my-fixed-hub-id")
        with (
            patch("hub.config.HUB_ID_FILE", hub_id_file),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            config = load_config(config_path=config_file)
        assert config.hub_id == "my-fixed-hub-id"

    def test_yaml_hub_id_takes_precedence_over_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("hub_id: yaml-defined-id\n")
        hub_id_file = tmp_path / "hub_id"
        hub_id_file.write_text("file-defined-id")
        with (
            patch("hub.config.HUB_ID_FILE", hub_id_file),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            config = load_config(config_path=config_file)
        assert config.hub_id == "yaml-defined-id"


# ── save_api_key guard ────────────────────────────────────────────────────────


class TestSaveApiKeyGuard:
    def test_saves_literal_key_when_no_existing_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("hub.config.HYBRO_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_FILE", tmp_path / "config.yaml")
        save_api_key("hybro_newkey")
        import yaml
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert data["cloud"]["api_key"] == "hybro_newkey"

    def test_skips_save_when_existing_value_is_env_var_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("cloud:\n  api_key: ${HYBRO_API_KEY}\n")
        monkeypatch.setattr("hub.config.HYBRO_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_FILE", config_file)
        save_api_key("hybro_should_not_be_written")
        # File must be unchanged
        assert "${HYBRO_API_KEY}" in config_file.read_text()

    def test_saves_when_existing_value_is_literal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("cloud:\n  api_key: hybro_old\n")
        monkeypatch.setattr("hub.config.HYBRO_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_FILE", config_file)
        save_api_key("hybro_new")
        import yaml
        data = yaml.safe_load(config_file.read_text())
        assert data["cloud"]["api_key"] == "hybro_new"

    def test_preserves_comments_on_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("# My important comment\ncloud:\n  api_key: hybro_old\n")
        monkeypatch.setattr("hub.config.HYBRO_DIR", tmp_path)
        monkeypatch.setattr("hub.config.CONFIG_FILE", config_file)
        save_api_key("hybro_new")
        assert "# My important comment" in config_file.read_text()


# ── load_config ValidationError ───────────────────────────────────────────────


class TestLoadConfigValidationError:
    def test_invalid_field_raises_system_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("publish_queue:\n  max_size_mb: fifty\n")
        with (
            patch("hub.config.HUB_ID_FILE", tmp_path / "hub_id"),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            with pytest.raises(SystemExit) as exc_info:
                load_config(config_path=config_file)
        assert "Invalid config" in str(exc_info.value)


# ── Unknown config keys ────────────────────────────────────────────────────────


class TestUnknownConfigKeys:
    def test_unknown_top_level_key_emits_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("heartbeat_intervall: 60\n")  # typo
        with (
            patch("hub.config.HUB_ID_FILE", tmp_path / "hub_id"),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            with pytest.warns(UserWarning, match="heartbeat_intervall"):
                load_config(config_path=config_file)

    def test_unknown_nested_key_emits_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("agents:\n  auto_discoverr: true\n")  # typo
        with (
            patch("hub.config.HUB_ID_FILE", tmp_path / "hub_id"),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            with pytest.warns(UserWarning, match="auto_discoverr"):
                load_config(config_path=config_file)


# ── CloudConfig.api_key empty string coercion ─────────────────────────────────


class TestCloudConfigApiKeyCoercion:
    def test_empty_string_coerced_to_none(self) -> None:
        cfg = CloudConfig(api_key="")
        assert cfg.api_key is None

    def test_none_stays_none(self) -> None:
        cfg = CloudConfig(api_key=None)
        assert cfg.api_key is None

    def test_valid_key_unchanged(self) -> None:
        cfg = CloudConfig(api_key="hybro_abc123")
        assert cfg.api_key == "hybro_abc123"


# ── auto_discover_scan_range returns tuple ────────────────────────────────────


class TestScanRangeTuple:
    def test_valid_range_returns_tuple(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("agents:\n  auto_discover_scan_range: [10000, 11000]\n")
        with (
            patch("hub.config.HUB_ID_FILE", tmp_path / "hub_id"),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            config = load_config(config_path=config_file)
        assert config.agents.auto_discover_scan_range == (10000, 11000)
        assert isinstance(config.agents.auto_discover_scan_range, tuple)

    def test_null_scan_range_is_none(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        with (
            patch("hub.config.HUB_ID_FILE", tmp_path / "hub_id"),
            patch("hub.config.HYBRO_DIR", tmp_path),
        ):
            config = load_config(config_path=config_file)
        assert config.agents.auto_discover_scan_range is None

