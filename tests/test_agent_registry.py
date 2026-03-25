"""Tests for hub.agent_registry."""

import io
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hub.agent_registry import (
    AgentRegistry,
    LocalAgent,
    _check_ipv6_available,
    _decode_proc_ip,
    _extract_capabilities,
    _get_listening_ports,
    _ports_connect_scan,
    _ports_linux,
    _ports_macos,
)
from hub.config import AgentsConfig, CloudConfig, HubConfig, LocalAgentConfig
import pydantic


@pytest.fixture
def config():
    return HubConfig(
        cloud=CloudConfig(api_key="test"),
        agents=AgentsConfig(
            local=[LocalAgentConfig(name="Test Agent", url="http://localhost:9001")],
            auto_discover=False,
        ),
    )


@pytest.fixture
def config_autodiscover():
    return HubConfig(
        cloud=CloudConfig(api_key="test"),
        agents=AgentsConfig(auto_discover=True),
    )


SAMPLE_CARD = {
    "name": "Sample Agent",
    "description": "A test agent",
    "url": "http://localhost:9001/",
    "version": "1.0.0",
    "capabilities": {"streaming": True},
    "skills": [{"id": "s1", "name": "Skill", "tags": ["chat"]}],
}


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_discover_manual_agent(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_CARD

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 1
        assert agents[0].name == "Test Agent"
        assert agents[0].agent_card == SAMPLE_CARD
        await registry.close()

    @pytest.mark.asyncio
    async def test_discover_unreachable_agent(self, config):
        registry = AgentRegistry(config)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_auto_discover(self, config_autodiscover):
        registry = AgentRegistry(config_autodiscover)

        async def mock_get(url, **kwargs):
            if "9001" in url and "agent-card.json" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = SAMPLE_CARD
                return resp
            raise httpx.ConnectError("refused")

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = mock_get
        registry._client = mock_client

        with patch(
            "hub.agent_registry._get_listening_ports",
            return_value=[9001, 9002, 9003],
        ):
            agents = await registry.discover()

        assert len(agents) == 1
        assert agents[0].url == "http://localhost:9001"
        await registry.close()

    @pytest.mark.asyncio
    async def test_auto_discover_no_ports(self, config_autodiscover):
        """When no ports are listening, auto-discovery finds nothing."""
        registry = AgentRegistry(config_autodiscover)

        with patch(
            "hub.agent_registry._get_listening_ports",
            return_value=[],
        ):
            agents = await registry.discover()

        assert len(agents) == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_discover_fallback_to_second_path(self, config):
        """Agent only serves at /.well-known/agent.json (deprecated path)."""
        registry = AgentRegistry(config)

        async def mock_get(url, **kwargs):
            if "agent.json" in url and "agent-card" not in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = SAMPLE_CARD
                return resp
            resp = MagicMock()
            resp.status_code = 404
            return resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = mock_get
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 1
        assert agents[0].agent_card == SAMPLE_CARD
        await registry.close()


class TestSyncPayload:
    @pytest.mark.asyncio
    async def test_to_sync_payload(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_CARD
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client

        await registry.discover()
        payload = registry.to_sync_payload()
        assert len(payload) == 1
        assert payload[0]["name"] == "Test Agent"
        assert payload[0]["agent_card"] == SAMPLE_CARD
        assert "local_agent_id" in payload[0]
        await registry.close()


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_marks_unhealthy(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_CARD
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client
        await registry.discover()
        assert len(registry.get_healthy_agents()) == 1

        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
        await registry.health_check()
        assert len(registry.get_healthy_agents()) == 0
        await registry.close()


class TestGetListeningPorts:
    """Tests for _get_listening_ports() platform dispatch and exclude logic.

    Each test patches the platform booleans and the individual strategy
    functions so the tests run correctly regardless of the host OS.
    """

    def _patch_platform(self, windows=False, macos=False, linux=False):
        return [
            patch("hub.agent_registry._WINDOWS", windows),
            patch("hub.agent_registry._MACOS", macos),
            patch("hub.agent_registry._LINUX", linux),
        ]

    def test_dispatches_to_windows_strategy(self):
        patches = self._patch_platform(windows=True)
        with patches[0], patches[1], patches[2], \
             patch("hub.agent_registry._ports_windows", return_value={8080, 3000}) as mock_w:
            ports = _get_listening_ports()
        mock_w.assert_called_once()
        assert ports == [3000, 8080]

    def test_dispatches_to_macos_strategy(self):
        patches = self._patch_platform(macos=True)
        with patches[0], patches[1], patches[2], \
             patch("hub.agent_registry._ports_macos", return_value={9001, 9002}) as mock_m:
            ports = _get_listening_ports()
        mock_m.assert_called_once()
        assert ports == [9001, 9002]

    def test_dispatches_to_linux_strategy(self):
        patches = self._patch_platform(linux=True)
        with patches[0], patches[1], patches[2], \
             patch("hub.agent_registry._ports_linux", return_value={4000}) as mock_l:
            ports = _get_listening_ports()
        mock_l.assert_called_once()
        assert ports == [4000]

    def test_falls_back_to_connect_scan_when_strategy_returns_none(self):
        patches = self._patch_platform(macos=True)
        with patches[0], patches[1], patches[2], \
             patch("hub.agent_registry._ports_macos", return_value=None), \
             patch("hub.agent_registry._ports_connect_scan", return_value={7000}) as mock_scan:
            ports = _get_listening_ports(scan_range=(7000, 7000))
        mock_scan.assert_called_once_with(7000, 7000)
        assert ports == [7000]

    def test_falls_back_to_connect_scan_on_unknown_platform(self):
        # All platform flags False → no strategy matches → connect-scan
        patches = self._patch_platform()
        with patches[0], patches[1], patches[2], \
             patch("hub.agent_registry._ports_connect_scan", return_value={5000}) as mock_scan:
            ports = _get_listening_ports(scan_range=(5000, 5000))
        mock_scan.assert_called_once()
        assert ports == [5000]

    def test_exclude_removes_ports(self):
        patches = self._patch_platform(linux=True)
        with patches[0], patches[1], patches[2], \
             patch("hub.agent_registry._ports_linux", return_value={22, 8080, 9001}):
            ports = _get_listening_ports(exclude={22})
        assert 22 not in ports
        assert ports == [8080, 9001]

    def test_returns_sorted(self):
        patches = self._patch_platform(linux=True)
        with patches[0], patches[1], patches[2], \
             patch("hub.agent_registry._ports_linux", return_value={9999, 1111, 5555}):
            ports = _get_listening_ports()
        assert ports == [1111, 5555, 9999]

    def test_empty_result_does_not_fall_back_to_scan(self):
        """Strategy returning empty set() should NOT trigger fallback."""
        patches = self._patch_platform(linux=True)
        with patches[0], patches[1], patches[2], \
             patch("hub.agent_registry._ports_linux", return_value=set()), \
             patch("hub.agent_registry._ports_connect_scan") as mock_scan:
            ports = _get_listening_ports()
        mock_scan.assert_not_called()
        assert ports == []


class TestPortsMacos:
    """Unit tests for _ports_macos()."""

    # Realistic lsof -iTCP -sTCP:LISTEN -n -P output
    LSOF_OUTPUT = (
        "COMMAND   PID  USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n"
        "python3  1234  user    7u  IPv4 0x...            0t0  TCP *:8080 (LISTEN)\n"
        "python3  1235  user    8u  IPv4 0x...            0t0  TCP 127.0.0.1:9001 (LISTEN)\n"
        "python3  1236  user    9u  IPv6 0x...            0t0  TCP [::1]:9002 (LISTEN)\n"
        "nginx     999  user   10u  IPv4 0x...            0t0  TCP 192.168.1.5:80 (LISTEN)\n"
        "nginx    1000  user   11u  IPv4 0x...            0t0  TCP 10.0.0.1:443 (LISTEN)\n"
    )

    def test_includes_wildcard_and_loopback(self):
        mock_result = MagicMock()
        mock_result.stdout = self.LSOF_OUTPUT
        with patch("subprocess.run", return_value=mock_result):
            ports = _ports_macos()
        assert ports == {8080, 9001, 9002}

    def test_excludes_external_ips(self):
        mock_result = MagicMock()
        mock_result.stdout = self.LSOF_OUTPUT
        with patch("subprocess.run", return_value=mock_result):
            ports = _ports_macos()
        assert 80 not in ports
        assert 443 not in ports

    def test_returns_none_when_lsof_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _ports_macos()
        assert result is None

    def test_returns_none_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=5)):
            result = _ports_macos()
        assert result is None

    def test_empty_output_returns_empty_set(self):
        mock_result = MagicMock()
        mock_result.stdout = "COMMAND  PID  USER  FD  TYPE  DEVICE  SIZE/OFF  NODE  NAME\n"
        with patch("subprocess.run", return_value=mock_result):
            assert _ports_macos() == set()


class TestPortsLinux:
    """Unit tests for _ports_linux() and _decode_proc_ip()."""

    # /proc/net/tcp format: sl local_address rem_address st ...
    # 127.0.0.1:8080 in little-endian hex: 0100007F:1F90
    # 0.0.0.0:9001   in little-endian hex: 00000000:2329
    # 192.168.1.5:80 in little-endian hex: 0501A8C0:0050
    PROC_NET_TCP = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000000000000000 100 0 0 10 0\n"
        "   1: 00000000:2329 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12346 1 0000000000000000 100 0 0 10 0\n"
        "   2: 0501A8C0:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12347 1 0000000000000000 100 0 0 10 0\n"
        "   3: 0100007F:2328 00000000:0000 06 00000000:00000000 00:00000000 00000000  1000        0 12348 1 0000000000000000 100 0 0 10 0\n"
    )

    def _mock_open(self, content: str, path: str = "/proc/net/tcp"):
        """Return a side_effect for builtins.open that serves fake /proc content."""
        def _open(p, *args, **kwargs):
            if p == path:
                return io.StringIO(content)
            raise OSError(f"No such file: {p}")
        return _open

    def test_includes_loopback_and_wildcard(self):
        with patch("builtins.open", side_effect=self._mock_open(self.PROC_NET_TCP)):
            ports = _ports_linux()
        # 0x1F90 = 8080, 0x2329 = 9001
        assert ports is not None
        assert 8080 in ports
        assert 9001 in ports

    def test_excludes_external_ip(self):
        with patch("builtins.open", side_effect=self._mock_open(self.PROC_NET_TCP)):
            ports = _ports_linux()
        assert ports is not None
        assert 80 not in ports  # bound to 192.168.1.5

    def test_excludes_non_listen_state(self):
        with patch("builtins.open", side_effect=self._mock_open(self.PROC_NET_TCP)):
            ports = _ports_linux()
        assert ports is not None
        # port 0x2328=9000 has state 06 (ESTABLISHED), not LISTEN
        assert 9000 not in ports

    def test_returns_none_when_proc_unavailable(self):
        with patch("builtins.open", side_effect=OSError("no /proc")):
            result = _ports_linux()
        assert result is None

    def test_decode_proc_ip_ipv4_loopback(self):
        assert _decode_proc_ip("0100007F") == "127.0.0.1"

    def test_decode_proc_ip_ipv4_wildcard(self):
        assert _decode_proc_ip("00000000") == "0.0.0.0"

    def test_decode_proc_ip_ipv4_external(self):
        assert _decode_proc_ip("0501A8C0") == "192.168.1.5"

    def test_decode_proc_ip_ipv6_loopback(self):
        # ::1 in /proc/net/tcp6 little-endian: 00000000000000000000000001000000
        result = _decode_proc_ip("00000000000000000000000001000000")
        assert result == "::1"


class TestPortsConnectScan:
    """Unit tests for _ports_connect_scan()."""

    def _make_mock_socket(self, open_on_ipv4=None, open_on_ipv6=None):
        """Return a socket constructor mock that simulates selective open ports.

        open_on_ipv4: port that returns 0 (success) on 127.0.0.1
        open_on_ipv6: port that returns 0 (success) on ::1
        """
        def socket_factory(family, *args, **kwargs):
            sock = MagicMock()
            sock.__enter__ = lambda s: s
            sock.__exit__ = MagicMock(return_value=False)

            if family == socket.AF_INET:
                def connect_ex_ipv4(addr):
                    return 0 if open_on_ipv4 and addr[1] == open_on_ipv4 else 111
                sock.connect_ex.side_effect = connect_ex_ipv4
            else:
                def connect_ex_ipv6(addr):
                    return 0 if open_on_ipv6 and addr[1] == open_on_ipv6 else 111
                sock.connect_ex.side_effect = connect_ex_ipv6

            return sock

        return socket_factory

    def test_detects_ipv4_open_port(self):
        with patch("socket.socket", side_effect=self._make_mock_socket(open_on_ipv4=9999)):
            ports = _ports_connect_scan(9998, 10000)
        assert 9999 in ports
        assert 9998 not in ports
        assert 10000 not in ports

    def test_detects_ipv6_only_port(self):
        """A port bound exclusively to ::1 must be detected via the IPv6 probe."""
        with patch("socket.socket", side_effect=self._make_mock_socket(open_on_ipv6=9001)):
            ports = _ports_connect_scan(9000, 9002)
        assert 9001 in ports
        assert 9000 not in ports

    def test_detects_port_open_on_both_families(self):
        with patch("socket.socket", side_effect=self._make_mock_socket(open_on_ipv4=8080, open_on_ipv6=8080)):
            ports = _ports_connect_scan(8080, 8080)
        assert 8080 in ports

    def test_ipv6_unavailable_does_not_raise(self):
        """If IPv6 is unavailable, scan completes using IPv4 only."""
        def ipv4_only_socket(family, *args, **kwargs):
            sock = MagicMock()
            sock.__enter__ = lambda s: s
            sock.__exit__ = MagicMock(return_value=False)
            sock.connect_ex.return_value = 111  # ECONNREFUSED
            return sock

        with patch("hub.agent_registry._check_ipv6_available", return_value=False), \
             patch("socket.socket", side_effect=ipv4_only_socket):
            ports = _ports_connect_scan(9000, 9001)
        assert ports == set()

    def test_check_ipv6_available_returns_true_when_supported(self):
        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.socket", return_value=mock_sock):
            assert _check_ipv6_available() is True

    def test_check_ipv6_available_returns_false_when_unsupported(self):
        with patch("socket.socket", side_effect=OSError("IPv6 not supported")):
            assert _check_ipv6_available() is False

    def test_empty_range_returns_empty(self):
        ports = _ports_connect_scan(10, 9)  # invalid range
        assert ports == set()


class TestExtractCapabilities:
    def test_extracts_streaming(self):
        caps = _extract_capabilities({"capabilities": {"streaming": True}})
        assert "streaming" in caps

    def test_extracts_skill_tags(self):
        caps = _extract_capabilities(
            {"skills": [{"tags": ["code", "review"]}]}
        )
        assert "code" in caps
        assert "review" in caps

    def test_empty_card(self):
        assert _extract_capabilities({}) == []


class TestScanRangeValidator:
    """Tests for HubConfig.auto_discover_scan_range validation."""

    def _make(self, scan_range):
        return HubConfig(agents=AgentsConfig(auto_discover_scan_range=scan_range))

    def test_none_is_valid(self):
        cfg = self._make(None)
        assert cfg.agents.auto_discover_scan_range is None

    def test_valid_range(self):
        cfg = self._make([8000, 9999])
        assert cfg.agents.auto_discover_scan_range == [8000, 9999]

    def test_equal_start_end_is_valid(self):
        cfg = self._make([9001, 9001])
        assert cfg.agents.auto_discover_scan_range == [9001, 9001]

    def test_boundary_ports_are_valid(self):
        cfg = self._make([0, 65535])
        assert cfg.agents.auto_discover_scan_range == [0, 65535]

    def test_too_few_elements(self):
        with pytest.raises(pydantic.ValidationError, match="exactly \\[start, end\\]"):
            self._make([7000])

    def test_too_many_elements(self):
        with pytest.raises(pydantic.ValidationError, match="exactly \\[start, end\\]"):
            self._make([1, 2, 3])

    def test_start_port_out_of_range(self):
        with pytest.raises(pydantic.ValidationError, match="start port -1"):
            self._make([-1, 9000])

    def test_end_port_out_of_range(self):
        with pytest.raises(pydantic.ValidationError, match="end port 65536"):
            self._make([1024, 65536])

    def test_start_greater_than_end(self):
        with pytest.raises(pydantic.ValidationError, match="start.*must be <= end"):
            self._make([9000, 8000])
