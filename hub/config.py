"""Hub configuration loader.

Loads settings from ~/.hybro/config.yaml with fallback to environment variables.
Manages hub_id persistence in ~/.hybro/hub_id.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from uuid import uuid4

from typing import Any, get_origin

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

HYBRO_DIR = Path.home() / ".hybro"
HUB_ID_FILE = HYBRO_DIR / "hub_id"
CONFIG_FILE = HYBRO_DIR / "config.yaml"


class LocalAgentConfig(BaseModel):
    """A manually-configured local A2A agent."""

    model_config = {"frozen": True}

    name: str
    url: str

    @model_validator(mode="before")
    @classmethod
    def _check_unknown_keys(cls, data: object) -> object:
        if isinstance(data, dict):
            _warn_unknown_keys(cls, data)
        return data


class PublishQueueConfig(BaseModel):
    """Configuration for the disk-backed publish queue."""

    model_config = {"frozen": True}

    enabled: bool = True
    max_size_mb: int = 50           # Max disk usage in MB
    ttl_hours: int = 24             # Events older than this are dropped
    drain_interval: int = 30        # Seconds between background drain cycles
    drain_batch_size: int = 20      # Max events processed per drain cycle

    # Per-category retry limits (power-user tuning)
    max_retries_critical: int = 20  # agent_response, agent_error, processing_status
    max_retries_normal: int = 5     # task_submitted, artifact_update, task_status

    @model_validator(mode="before")
    @classmethod
    def _check_unknown_keys(cls, data: object) -> object:
        if isinstance(data, dict):
            _warn_unknown_keys(cls, data)
        return data


def _coerce_nulls(model_cls: type[BaseModel], data: dict) -> dict:
    """Coerce None → [] for any field annotated as list[X].

    Guards against YAML `key: null` (parsed as None) bypassing field defaults.
    Union-typed fields like list[X] | None are left untouched because their
    get_origin is not `list`.
    """
    for name, field in model_cls.model_fields.items():
        if data.get(name) is None:
            origin = get_origin(field.annotation)
            if origin is list:
                data[name] = []
    return data


def _warn_unknown_keys(model_cls: type[BaseModel], data: dict) -> None:
    """Emit a UserWarning for any key not declared in the model.

    Helps users catch typos (e.g. heartbeat_intervall) that would otherwise
    be silently ignored by Pydantic's default extra="ignore" behaviour.
    """
    import warnings

    known = set(model_cls.model_fields.keys())
    for key in data:
        if key not in known:
            warnings.warn(
                f"Unknown config field '{key}' in {model_cls.__name__} — "
                "check for typos (field will be ignored)",
                UserWarning,
                stacklevel=6,
            )


class CloudConfig(BaseModel):
    """Cloud connectivity settings."""

    model_config = {"frozen": True}

    api_key: str | None = None
    gateway_url: str = "https://api.hybro.ai"

    @model_validator(mode="before")
    @classmethod
    def _coerce_nulls(cls, data: object) -> object:
        if isinstance(data, dict):
            _warn_unknown_keys(cls, data)
            return _coerce_nulls(cls, data)
        return data

    @field_validator("api_key")
    @classmethod
    def _coerce_empty_api_key(cls, v: str | None) -> str | None:
        return None if v == "" else v


class AgentsConfig(BaseModel):
    """Agent discovery settings."""

    model_config = {"frozen": True}

    local: list[LocalAgentConfig] = Field(default_factory=list)
    auto_discover: bool = True
    auto_discover_exclude_ports: list[int] = Field(
        default_factory=lambda: [22, 53, 80, 443, 3306, 5432, 6379, 27017],
    )
    # Optional (start, end) range for the connect-scan fallback strategy.
    # When null the full unprivileged range (1024–65535) is used.
    auto_discover_scan_range: tuple[int, int] | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_nulls(cls, data: object) -> object:
        if isinstance(data, dict):
            _warn_unknown_keys(cls, data)
            return _coerce_nulls(cls, data)
        return data

    @field_validator("auto_discover_scan_range", mode="before")
    @classmethod
    def _validate_scan_range(cls, v: object) -> tuple[int, int] | None:
        if v is None:
            return None
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise ValueError(
                f"auto_discover_scan_range must be exactly [start, end], got {v!r}"
            )
        start, end = int(v[0]), int(v[1])
        for name, port in (("start", start), ("end", end)):
            if not (0 <= port <= 65535):
                raise ValueError(
                    f"auto_discover_scan_range {name} port {port} is out of range 0–65535"
                )
        if start > end:
            raise ValueError(
                f"auto_discover_scan_range start ({start}) must be <= end ({end})"
            )
        return (start, end)


class PrivacyConfig(BaseModel):
    """Privacy and routing settings."""

    model_config = {"frozen": True}

    sensitive_keywords: list[str] = Field(default_factory=list)
    sensitive_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_nulls(cls, data: object) -> object:
        if isinstance(data, dict):
            _warn_unknown_keys(cls, data)
            return _coerce_nulls(cls, data)
        return data


class HubConfig(BaseModel):
    """Hub daemon configuration."""

    model_config = {"frozen": True}

    hub_id: str = ""
    heartbeat_interval: int = 30
    dispatcher_read_timeout_secs: int = 300

    cloud: CloudConfig = Field(default_factory=CloudConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    publish_queue: PublishQueueConfig = Field(default_factory=PublishQueueConfig)

    @model_validator(mode="before")
    @classmethod
    def _check_unknown_keys(cls, data: object) -> object:
        if isinstance(data, dict):
            _warn_unknown_keys(cls, data)
        return data

    @field_validator("hub_id")
    @classmethod
    def _validate_hub_id(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "hub_id must not be empty — use load_config() to construct HubConfig"
            )
        return v


def _expand_env_vars(text: str) -> str:
    """Expand ${VAR} and ${VAR:-default} references before YAML parsing.

    Matches the OTel Collector / Grafana Agent convention:
      ${VAR}           — value of VAR, or "" if unset
      ${VAR:-default}  — value of VAR, or "default" if unset
      $${VAR}          — literal ${VAR} (escape)

    LIMITATION: env var values must not contain YAML special characters
    (#, colon-space, {, }, [, ], |, >) as expansion happens before parsing.
    """
    def _replace(m: re.Match) -> str:
        if m.group(1) is None:
            # Matched $${ escape branch — strip the leading $ to produce ${...}
            return m.group(0)[1:]
        return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")

    # The $${ branch has no capture groups so group(1) is None when it matches,
    # which distinguishes it from the expansion branch.
    return re.sub(r'\$\$\{[^}]*\}|\$\{([^}:-]+)(?::-([^}]*))?\}', _replace, text)


def load_config(
    api_key: str | None = None,
    config_path: Path | None = None,
) -> HubConfig:
    """Load hub configuration from YAML file, env vars, and CLI args.

    Priority (highest to lowest):
        1. CLI arguments (api_key param)
        2. Environment variables (HYBRO_API_KEY, HYBRO_GATEWAY_URL)
        3. YAML config file (~/.hybro/config.yaml)
        4. Defaults
    """
    data: dict = {}
    path = config_path or CONFIG_FILE

    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(_expand_env_vars(f.read())) or {}
        if isinstance(raw, dict):
            data = raw
        logger.debug("Loaded config from %s", path)

    # Env var overrides — inject into the cloud sub-dict so the nested model
    # picks them up at the right level.
    cloud = data.setdefault("cloud", {})
    if env_key := os.environ.get("HYBRO_API_KEY"):
        cloud["api_key"] = env_key
    if env_gw := os.environ.get("HYBRO_GATEWAY_URL"):
        cloud["gateway_url"] = env_gw

    # CLI arg override
    if api_key is not None:
        cloud["api_key"] = api_key

    # Resolve hub_id before construction (model is frozen after)
    if not data.get("hub_id"):
        data["hub_id"] = _load_or_create_hub_id()

    try:
        return HubConfig(**data)
    except ValidationError as exc:
        lines = [f"  {e['loc'][-1]}: {e['msg']}" for e in exc.errors()]
        raise SystemExit(
            f"Invalid config ({path}):\n" + "\n".join(lines)
        ) from None


def _load_or_create_hub_id() -> str:
    """Load hub_id from disk or create a new one."""
    HYBRO_DIR.mkdir(parents=True, exist_ok=True)
    if HUB_ID_FILE.exists():
        hub_id = HUB_ID_FILE.read_text().strip()
        if hub_id:
            return hub_id
    hub_id = uuid4().hex
    HUB_ID_FILE.write_text(hub_id)
    logger.info("Generated new hub_id: %s (saved to %s)", hub_id, HUB_ID_FILE)
    return hub_id


def save_api_key(api_key: str) -> None:
    """Persist API key to config file, preserving existing comments and formatting."""
    HYBRO_DIR.mkdir(parents=True, exist_ok=True)
    ryaml = YAML()
    ryaml.preserve_quotes = True
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            data: Any = ryaml.load(f) or {}
    else:
        data = {}
    # If the existing value is an env var reference, don't overwrite it —
    # the user deliberately chose env var wiring and saving a literal key
    # would silently destroy that setup.
    existing = data.get("cloud", {}).get("api_key", "")
    if isinstance(existing, str) and existing.startswith("${"):
        logger.info("Skipping api_key save — existing value is an env var reference (%s)", existing)
        return
    if "cloud" not in data:
        data["cloud"] = {}
    data["cloud"]["api_key"] = api_key
    with open(CONFIG_FILE, "w") as f:
        ryaml.dump(data, f)
    logger.info("API key saved to %s", CONFIG_FILE)
