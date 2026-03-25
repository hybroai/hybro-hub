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

from typing import IO, Any, get_origin

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

HYBRO_DIR = Path.home() / ".hybro"
HUB_ID_FILE = HYBRO_DIR / "hub_id"
CONFIG_FILE = HYBRO_DIR / "config.yaml"


class LocalAgentConfig(BaseModel):
    """A manually-configured local A2A agent."""

    model_config = {"frozen": True}

    name: str
    url: str


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


class CloudConfig(BaseModel):
    """Cloud connectivity settings."""

    model_config = {"frozen": True}

    api_key: str | None = None
    gateway_url: str = "https://api.hybro.ai"

    @model_validator(mode="before")
    @classmethod
    def _coerce_nulls(cls, data: object) -> object:
        return _coerce_nulls(cls, data) if isinstance(data, dict) else data


class AgentsConfig(BaseModel):
    """Agent discovery settings."""

    model_config = {"frozen": True}

    local: list[LocalAgentConfig] = Field(default_factory=list)
    auto_discover: bool = True
    auto_discover_exclude_ports: list[int] = Field(
        default_factory=lambda: [22, 53, 80, 443, 3306, 5432, 6379, 27017],
    )
    # Optional [start, end] range for the connect-scan fallback strategy.
    # When null the full unprivileged range (1024–65535) is used.
    auto_discover_scan_range: list[int] | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_nulls(cls, data: object) -> object:
        return _coerce_nulls(cls, data) if isinstance(data, dict) else data

    @field_validator("auto_discover_scan_range")
    @classmethod
    def _validate_scan_range(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        if len(v) != 2:
            raise ValueError(
                f"auto_discover_scan_range must be exactly [start, end], got {v!r}"
            )
        start, end = v
        for name, port in (("start", start), ("end", end)):
            if not (0 <= port <= 65535):
                raise ValueError(
                    f"auto_discover_scan_range {name} port {port} is out of range 0–65535"
                )
        if start > end:
            raise ValueError(
                f"auto_discover_scan_range start ({start}) must be <= end ({end})"
            )
        return v


class PrivacyConfig(BaseModel):
    """Privacy and routing settings."""

    model_config = {"frozen": True}

    sensitive_keywords: list[str] = Field(default_factory=list)
    sensitive_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_nulls(cls, data: object) -> object:
        return _coerce_nulls(cls, data) if isinstance(data, dict) else data


class HubConfig(BaseModel):
    """Hub daemon configuration."""

    model_config = {"frozen": True}

    hub_id: str = ""
    heartbeat_interval: int = 30

    cloud: CloudConfig = Field(default_factory=CloudConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    publish_queue: PublishQueueConfig = Field(default_factory=PublishQueueConfig)


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

    return HubConfig(**data)


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


LOCK_FILE = HYBRO_DIR / "hub.lock"
LOG_FILE = HYBRO_DIR / "hub.log"


def acquire_instance_lock() -> IO[Any]:
    """Acquire an exclusive lock on ~/.hybro/hub.lock.

    Returns the open file object — it must stay open for the lock to be held.
    The file handle is inherited across fork() so the daemon child keeps the lock
    after the parent exits.  Call write_lock_pid() in the child once its final PID
    is known.

    Raises SystemExit with a clear message if another instance is already running.
    """
    HYBRO_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh: IO[Any] = open(LOCK_FILE, "w", encoding="utf-8")  # noqa: SIM115

    try:
        import fcntl  # Unix only
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        # Windows: use msvcrt
        import msvcrt
        try:
            msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock_fh.close()
            import sys
            logger.error(
                "Another hybro-hub instance is already running on this machine. "
                "Stop it before starting a new one."
            )
            sys.exit(1)
    except OSError:
        lock_fh.close()
        import sys
        logger.error(
            "Another hybro-hub instance is already running on this machine. "
            "Stop it before starting a new one."
        )
        sys.exit(1)

    return lock_fh


def write_lock_pid(lock_fh: IO[Any]) -> None:
    """Write (or overwrite) the current process's PID into the lock file."""
    lock_fh.seek(0)
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()


def read_lock_pid() -> int | None:
    """Read the daemon PID from the lock file. Returns None if not found."""
    try:
        text = LOCK_FILE.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def save_api_key(api_key: str) -> None:
    """Persist API key to config file."""
    HYBRO_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            data = yaml.safe_load(f) or {}
    # If the existing value is an env var reference, don't overwrite it —
    # the user deliberately chose env var wiring and saving a literal key
    # would silently destroy that setup.
    existing = data.get("cloud", {}).get("api_key", "")
    if isinstance(existing, str) and existing.startswith("${"):
        logger.info("Skipping api_key save — existing value is an env var reference (%s)", existing)
        return
    data.setdefault("cloud", {})["api_key"] = api_key
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    logger.info("API key saved to %s", CONFIG_FILE)
