"""Agent registry — discover and health-check local A2A agents.

Supports manual configuration (config.yaml) and auto-discovery
(enumerates listening localhost ports without elevated privileges,
then probes for well-known A2A agent card paths).

Port enumeration strategy (no sudo/root required):
  - Windows : psutil.net_connections() — works unprivileged on Windows
  - macOS   : ``lsof`` — built into macOS, always available
  - Linux   : /proc/net/tcp pseudo-file — always present, world-readable
  - Fallback: TCP connect-scan over a configurable port range
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import httpx
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

from . import a2a_compat
from .a2a_compat import ResolvedInterface
from .config import HubConfig

logger = logging.getLogger(__name__)

# Legacy path used by older A2A deployments (removed from a2a-sdk re-exports).
_LEGACY_AGENT_CARD_WELL_KNOWN_PATH = "/.well-known/agent.json"

AGENT_CARD_PATHS = [
    AGENT_CARD_WELL_KNOWN_PATH,  # /.well-known/agent-card.json
    _LEGACY_AGENT_CARD_WELL_KNOWN_PATH,
]
DISCOVERY_TIMEOUT = 3

_LOCALHOST_ADDRS = frozenset({"127.0.0.1", "::1", "0.0.0.0", "::", ""})

_CONCURRENCY_LIMIT = 30


def _normalize_url(url: str) -> str:
    """Canonicalize localhost aliases so the same agent always maps to one URL."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname in _LOCALHOST_ADDRS:
        netloc = f"localhost:{parsed.port}" if parsed.port else "localhost"
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed)


@dataclass
class LocalAgent:
    """A discovered local A2A agent."""

    local_agent_id: str
    name: str
    url: str
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    agent_card: dict = field(default_factory=dict)
    healthy: bool = True
    interface: ResolvedInterface | None = None
    fallback_interface: ResolvedInterface | None = None

    def __post_init__(self) -> None:
        if self.interface is None:
            self.interface = ResolvedInterface(
                binding="JSONRPC",
                protocol_version="0.3",
                url=self.url,
            )


class AgentRegistry:
    """Registry of local A2A agents."""

    HEALTH_FAILURE_THRESHOLD = 3

    def __init__(self, config: HubConfig) -> None:
        self._config = config
        self._agents: dict[str, LocalAgent] = {}
        self._client: httpx.AsyncClient | None = None
        self._failure_counts: dict[str, int] = {}

    @property
    def agents(self) -> dict[str, LocalAgent]:
        return dict(self._agents)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ──── Discovery ────

    async def discover(self) -> list[LocalAgent]:
        """Run full discovery: manual config + auto-discovery."""
        for ac in self._config.agents.local:
            await self._probe_and_register(ac.url, ac.name)

        if self._config.agents.auto_discover:
            await self._auto_discover()

        logger.info("Registry has %d agents", len(self._agents))
        return list(self._agents.values())

    async def _auto_discover(self) -> None:
        """Discover A2A agents by enumerating listening localhost ports."""
        scan_range: tuple[int, int] | None = self._config.agents.auto_discover_scan_range

        ports = _get_listening_ports(
            exclude=set(self._config.agents.auto_discover_exclude_ports),
            scan_range=scan_range,
        )
        if not ports:
            logger.debug("No listening localhost ports found for auto-discovery")
            return

        logger.debug("Auto-discovery: probing %d listening ports", len(ports))
        sem = asyncio.Semaphore(_CONCURRENCY_LIMIT)

        async def _bounded_probe(port: int) -> None:
            async with sem:
                await self._probe_and_register(
                    f"http://localhost:{port}", source="auto"
                )

        await asyncio.gather(
            *(_bounded_probe(p) for p in ports),
            return_exceptions=True,
        )

    async def _probe_and_register(
        self, url: str, name: str | None = None, source: str = "config"
    ) -> LocalAgent | None:
        """Try to fetch an agent card from a URL and register it."""
        url = _normalize_url(url.rstrip("/"))
        card = await self._fetch_agent_card(url, source)
        if card is None:
            return None

        try:
            interface = a2a_compat.select_interface(card)
        except ValueError:
            logger.debug("Agent card at %s has no usable JSON-RPC interface", url)
            return None
        fallback = a2a_compat.select_fallback_interface(card, interface)

        agent_name = name or card.get("name", f"Agent@{url}")
        existing = next(
            (a for a in self._agents.values() if a.url == url), None
        )
        if existing:
            existing.agent_card = card
            existing.healthy = True
            existing.name = agent_name
            existing.description = card.get("description", "")
            existing.capabilities = _extract_capabilities(card)
            existing.interface = interface
            existing.fallback_interface = fallback
            self._failure_counts.pop(existing.local_agent_id, None)
            return existing

        local_id = hashlib.sha256(url.encode()).hexdigest()[:12]
        agent = LocalAgent(
            local_agent_id=local_id,
            name=agent_name,
            url=url,
            description=card.get("description", ""),
            capabilities=_extract_capabilities(card),
            agent_card=card,
            healthy=True,
            interface=interface,
            fallback_interface=fallback,
        )
        self._agents[local_id] = agent
        logger.info(
            "Discovered agent: %s at %s (id=%s, protocol=%s)",
            agent_name, url, local_id, interface.protocol_version,
        )
        return agent

    # ──── Health check ────

    async def health_check(self) -> None:
        """Ping all registered agents and update health status.

        After ``HEALTH_FAILURE_THRESHOLD`` consecutive failures an agent is
        removed from the registry so the next sync will cause the backend to
        mark it inactive.
        """
        for agent in list(self._agents.values()):
            card = await self._fetch_agent_card(agent.url)
            if card is not None:
                try:
                    new_iface = a2a_compat.select_interface(card)
                except ValueError:
                    logger.warning(
                        "Agent %s returned card with no usable interface — marking unhealthy",
                        agent.name,
                    )
                    agent.healthy = False
                    count = self._failure_counts.get(agent.local_agent_id, 0) + 1
                    self._failure_counts[agent.local_agent_id] = count
                    if count >= self.HEALTH_FAILURE_THRESHOLD:
                        logger.warning(
                            "Agent %s failed %d consecutive health checks — removing",
                            agent.name, count,
                        )
                        del self._agents[agent.local_agent_id]
                        self._failure_counts.pop(agent.local_agent_id, None)
                    continue
                agent.healthy = True
                agent.agent_card = card
                agent.description = card.get("description", "")
                agent.capabilities = _extract_capabilities(card)
                agent.interface = new_iface
                agent.fallback_interface = a2a_compat.select_fallback_interface(
                    card, new_iface,
                )
                self._failure_counts.pop(agent.local_agent_id, None)
            else:
                agent.healthy = False
                count = self._failure_counts.get(agent.local_agent_id, 0) + 1
                self._failure_counts[agent.local_agent_id] = count
                if count >= self.HEALTH_FAILURE_THRESHOLD:
                    logger.warning(
                        "Agent %s failed %d consecutive health checks — removing",
                        agent.name,
                        count,
                    )
                    del self._agents[agent.local_agent_id]
                    self._failure_counts.pop(agent.local_agent_id, None)
                else:
                    logger.debug(
                        "Agent %s unhealthy (%d/%d)",
                        agent.name,
                        count,
                        self.HEALTH_FAILURE_THRESHOLD,
                    )

    # ──── Agent card fetch ────

    async def _fetch_agent_card(self, url: str, source: str = "config") -> dict | None:
        """Try each well-known agent card path and return the first valid card.

        Validates the response with a2a_compat.validate_agent_card() to reject
        non-agent HTTP 200 responses (e.g. error JSON from unrelated servers).
        """
        client = await self._get_client()
        for path in AGENT_CARD_PATHS:
            try:
                resp = await client.get(f"{url}{path}")
                if resp.status_code == 200:
                    card_data = resp.json()
                    if a2a_compat.validate_agent_card(card_data) is not None:
                        return card_data
            except Exception:
                continue
        if source == "config":
            logger.debug("Agent at %s not reachable", url)
        return None

    # ──── Lookup ────

    def get_agent(self, local_agent_id: str) -> LocalAgent | None:
        return self._agents.get(local_agent_id)

    def remove_agent(self, local_agent_id: str) -> bool:
        """Remove an agent from the registry. Returns True if it was present."""
        self._failure_counts.pop(local_agent_id, None)
        if local_agent_id in self._agents:
            del self._agents[local_agent_id]
            return True
        return False

    def get_healthy_agents(self) -> list[LocalAgent]:
        return [a for a in self._agents.values() if a.healthy]

    # ──── Sync payload ────

    def to_sync_payload(self) -> list[dict]:
        """Convert registry to HubAgentSyncRequest format.

        Includes all known agents regardless of health status so that
        a transient health-check failure doesn't cause the cloud to
        prune agents that still exist locally.
        """
        return [
            {
                "local_agent_id": a.local_agent_id,
                "name": a.name,
                "description": a.description,
                "capabilities": a.capabilities,
                "agent_card": a.agent_card,
            }
            for a in self._agents.values()
        ]


# ──── Port enumeration ────

# Sentinel values so callers can detect which strategy succeeded.
_WINDOWS = sys.platform == "win32"
_MACOS   = sys.platform == "darwin"
_LINUX   = sys.platform.startswith("linux")

# Port range used by the TCP connect-scan fallback.
_SCAN_DEFAULT_START = 1024
_SCAN_DEFAULT_END   = 65535
_SCAN_MAX_WORKERS   = 128
_SCAN_TIMEOUT       = 0.05  # seconds per probe


def _get_listening_ports(
    exclude: set[int] | None = None,
    scan_range: tuple[int, int] | None = None,
) -> list[int]:
    """Return sorted list of TCP ports listening on localhost.

    Tries OS-specific unprivileged strategies in order, falling back to a
    pure-Python TCP connect-scan.  No elevated privileges (sudo/admin) are
    required on any supported platform.

    Args:
        exclude:    Port numbers to omit from the result.
        scan_range: ``(start, end)`` inclusive range for the connect-scan
                    fallback.  Defaults to 1024–65535.
    """
    exclude = exclude or set()

    raw_ports: set[int] | None = None

    if _WINDOWS:
        raw_ports = _ports_windows()
    elif _MACOS:
        raw_ports = _ports_macos()
    elif _LINUX:
        raw_ports = _ports_linux()

    if raw_ports is None:
        # Either an unsupported platform or every strategy above failed —
        # fall back to a TCP connect-scan.
        start, end = scan_range or (_SCAN_DEFAULT_START, _SCAN_DEFAULT_END)
        logger.debug(
            "Port enumeration falling back to TCP connect-scan (%d–%d)", start, end
        )
        raw_ports = _ports_connect_scan(start, end)

    return sorted(raw_ports - exclude)


# ── Windows ──────────────────────────────────────────────────────────────────

def _ports_windows() -> set[int] | None:
    """Use psutil on Windows — no elevation required (GetExtendedTcpTable is
    accessible to normal user processes on Windows)."""
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("psutil not available; skipping Windows strategy")
        return None

    ports: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            addr, port = conn.laddr
            if addr in _LOCALHOST_ADDRS:
                ports.add(port)
    except Exception as exc:
        logger.debug("psutil.net_connections() failed on Windows: %s", exc)
        return None

    return ports


# ── macOS ─────────────────────────────────────────────────────────────────────

# lsof is a Core OS component on macOS — always at /usr/sbin/lsof.
_LSOF_BIN = "/usr/sbin/lsof"
# Match the NAME column: host (*, localhost, 127.0.0.1, [::1], etc.)
# followed by a colon, port number, and " (LISTEN)".
# Group 1: host   Group 2: port number
_LSOF_RE = re.compile(r"(?:^|[\s])(\S+):(\d+) \(LISTEN\)")

# Hosts that lsof reports for wildcard / loopback binds we care about.
# lsof -n wraps IPv6 addresses in brackets (e.g. [::1]), so bare "::1" never
# appears in the NAME column and is intentionally absent here.
_LSOF_LOCALHOST_HOSTS = frozenset({
    "*",          # wildcard — reachable on any interface including localhost
    "localhost",
    "127.0.0.1",
    "[::1]",
})


def _ports_macos() -> set[int] | None:
    """Parse ``lsof`` output — works without root for the current user's
    sockets, which is sufficient because agents are launched by the same
    user running the hub.

    Only ports whose host is a wildcard or a loopback address are returned.
    """
    try:
        # lsof COMMAND/NAME can contain non-UTF-8 bytes (paths, legacy encodings).
        # Strict decoding would raise UnicodeDecodeError and break auto-discover.
        result = subprocess.run(
            [_LSOF_BIN, "-iTCP", "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("lsof unavailable or timed out: %s", exc)
        return None

    ports: set[int] = set()
    for line in result.stdout.splitlines()[1:]:  # skip header
        m = _LSOF_RE.search(line)
        if not m:
            continue
        host, port_str = m.group(1), m.group(2)
        if host in _LSOF_LOCALHOST_HOSTS:
            ports.add(int(port_str))

    return ports


# ── Linux ─────────────────────────────────────────────────────────────────────

# /proc/net/tcp{,6} are world-readable kernel pseudo-files — always present
# on any Linux system with a network stack, including minimal containers.
_PROC_TCP_PATHS = ("/proc/net/tcp", "/proc/net/tcp6")
_TCP_LISTEN_STATE = 0x0A


def _decode_proc_ip(hex_addr: str) -> str:
    """Decode a hex-encoded IP address from /proc/net/tcp[6].

    /proc/net/tcp  stores IPv4 as a little-endian 32-bit hex string, e.g.
    ``0100007F`` → ``127.0.0.1``.
    /proc/net/tcp6 stores IPv6 as four little-endian 32-bit words, e.g.
    ``00000000000000000000000001000000`` → ``::1``.
    """
    if len(hex_addr) == 8:
        # IPv4: 4 bytes, little-endian
        addr_bytes = bytes.fromhex(hex_addr)[::-1]
        return socket.inet_ntop(socket.AF_INET, addr_bytes)
    else:
        # IPv6: 16 bytes stored as four little-endian 32-bit words
        raw = bytes.fromhex(hex_addr)
        # Reverse each 4-byte chunk independently
        addr_bytes = b"".join(raw[i:i+4][::-1] for i in range(0, 16, 4))
        return socket.inet_ntop(socket.AF_INET6, addr_bytes)


def _ports_linux() -> set[int] | None:
    """Parse ``/proc/net/tcp`` and ``/proc/net/tcp6`` directly.

    Each non-header row has the form:
        sl  local_address rem_address st ...
    where ``local_address`` is ``<hex-ip>:<hex-port>`` and ``st`` is the
    TCP state in hex (0x0A == TCP_LISTEN).

    Only ports bound to localhost addresses are returned, matching the
    behaviour of the Windows and macOS strategies.
    """
    ports: set[int] = set()
    found_any_file = False

    for path in _PROC_TCP_PATHS:
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue

        found_any_file = True
        for line in lines[1:]:  # skip header
            fields = line.split()
            if len(fields) < 4:
                continue
            state = int(fields[3], 16)
            if state != _TCP_LISTEN_STATE:
                continue
            ip_hex, port_hex = fields[1].split(":")
            try:
                ip_str = _decode_proc_ip(ip_hex)
            except (ValueError, OSError):
                continue
            if ip_str not in _LOCALHOST_ADDRS:
                continue
            ports.add(int(port_hex, 16))

    return ports if found_any_file else None


# ── Connect-scan fallback ─────────────────────────────────────────────────────

def _ports_connect_scan(start: int, end: int) -> set[int]:
    """Probe every port in [start, end] with a non-blocking TCP connect.

    This is the universal fallback — requires no OS files, no external
    commands, and no elevated privileges.  It is slower than the
    OS-specific strategies but parallelised with a thread pool.

    Both 127.0.0.1 (IPv4) and ::1 (IPv6) are probed so that agents bound
    exclusively to the IPv6 loopback are detected, consistent with how the
    OS-specific strategies treat ::1 as a local address.
    """
    # Check IPv6 availability once, before spawning threads, to avoid a
    # data race on the cached flag inside worker threads.
    ipv6_available = _check_ipv6_available()

    def _probe(port: int) -> int | None:
        # IPv4 probe
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(_SCAN_TIMEOUT)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return port

        # IPv6 probe — only if the OS has IPv6 support
        if ipv6_available:
            try:
                with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
                    s.settimeout(_SCAN_TIMEOUT)
                    # connect_ex for IPv6 takes (host, port, flowinfo, scope_id)
                    if s.connect_ex(("::1", port, 0, 0)) == 0:
                        return port
            except OSError:
                pass

        return None

    ports: set[int] = set()
    with ThreadPoolExecutor(max_workers=_SCAN_MAX_WORKERS) as pool:
        for result in pool.map(_probe, range(start, end + 1)):
            if result is not None:
                ports.add(result)
    return ports


def _check_ipv6_available() -> bool:
    """Return True if the OS can create an AF_INET6 socket."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM):
            pass
        return True
    except OSError:
        return False


def _extract_capabilities(card: dict) -> list[str]:
    """Extract capability list from an agent card."""
    caps = []
    abilities = card.get("capabilities", {})
    if abilities.get("streaming"):
        caps.append("streaming")
    if abilities.get("pushNotifications"):
        caps.append("push_notifications")
    skills = card.get("skills", [])
    for s in skills:
        if tags := s.get("tags"):
            caps.extend(tags)
    return caps
