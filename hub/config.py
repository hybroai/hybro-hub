"""Hub configuration loader.

Loads settings from ~/.hybro/config.yaml with fallback to environment variables.
Manages hub_id persistence in ~/.hybro/hub_id.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

HYBRO_DIR = Path.home() / ".hybro"
HUB_ID_FILE = HYBRO_DIR / "hub_id"
CONFIG_FILE = HYBRO_DIR / "config.yaml"


class LocalAgentConfig(BaseModel):
    """A manually-configured local A2A agent."""

    name: str
    url: str


class PublishQueueConfig(BaseModel):
    """Configuration for the disk-backed publish queue."""

    enabled: bool = True
    max_size_mb: int = 50           # Max disk usage in MB
    ttl_hours: int = 24             # Events older than this are dropped
    drain_interval: int = 30        # Seconds between background drain cycles
    drain_batch_size: int = 20      # Max events processed per drain cycle

    # Per-category retry limits (power-user tuning)
    max_retries_critical: int = 20  # agent_response, agent_error, processing_status
    max_retries_normal: int = 5     # task_submitted, artifact_update, task_status
    max_retries_streaming: int = 3  # Unused: retained for config-file backward compat


class HubConfig(BaseModel):
    """Hub daemon configuration."""

    api_key: str = ""
    gateway_url: str = "https://api.hybro.ai"
    hub_id: str = ""

    agents: list[LocalAgentConfig] = Field(default_factory=list)
    auto_discover: bool = True
    auto_discover_exclude_ports: list[int] = Field(
        default_factory=lambda: [22, 53, 80, 443, 3306, 5432, 6379, 27017],
    )
    # Optional [start, end] range for the connect-scan fallback strategy.
    # When null the full unprivileged range (1024–65535) is used.
    auto_discover_scan_range: list[int] | None = None

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

    privacy_default_routing: str = "local_first"
    privacy_sensitive_keywords: list[str] = Field(default_factory=list)
    privacy_sensitive_patterns: list[str] = Field(default_factory=list)

    heartbeat_interval: int = 30

    publish_queue: PublishQueueConfig = Field(default_factory=PublishQueueConfig)


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
            raw = yaml.safe_load(f) or {}
        if isinstance(raw, dict):
            data = raw
            # Flatten nested keys for Pydantic
            cloud = data.pop("cloud", {}) or {}
            if "api_key" in cloud:
                data.setdefault("api_key", cloud["api_key"])
            if "gateway_url" in cloud:
                data.setdefault("gateway_url", cloud["gateway_url"])
            privacy = data.pop("privacy", {}) or {}
            for k, v in privacy.items():
                data.setdefault(f"privacy_{k}", v)
            # Flatten agents.local -> agents
            agents_section = data.pop("agents", None)
            if isinstance(agents_section, dict):
                data["agents"] = agents_section.get("local", [])
                if "auto_discover" in agents_section:
                    data["auto_discover"] = agents_section["auto_discover"]
                if "auto_discover_exclude_ports" in agents_section:
                    data["auto_discover_exclude_ports"] = agents_section[
                        "auto_discover_exclude_ports"
                    ]
                if "auto_discover_scan_range" in agents_section:
                    data["auto_discover_scan_range"] = agents_section[
                        "auto_discover_scan_range"
                    ]
        logger.debug("Loaded config from %s", path)

    # Env var overrides
    if env_key := os.environ.get("HYBRO_API_KEY"):
        data["api_key"] = env_key
    if env_gw := os.environ.get("HYBRO_GATEWAY_URL"):
        data["gateway_url"] = env_gw

    # CLI arg override
    if api_key:
        data["api_key"] = api_key

    config = HubConfig(**data)

    # Resolve hub_id
    if not config.hub_id:
        config.hub_id = _load_or_create_hub_id()

    return config


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


def acquire_instance_lock() -> "IO[Any]":
    """Acquire an exclusive lock on ~/.hybro/hub.lock.

    Returns the open file object — it must stay open for the lock to be held.
    The file handle is inherited across fork() so the daemon child keeps the lock
    after the parent exits.  Call write_lock_pid() in the child once its final PID
    is known.

    Raises SystemExit with a clear message if another instance is already running.
    """
    from typing import IO, Any

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


def write_lock_pid(lock_fh: "IO[Any]") -> None:
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
    data.setdefault("cloud", {})["api_key"] = api_key
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    logger.info("API key saved to %s", CONFIG_FILE)
