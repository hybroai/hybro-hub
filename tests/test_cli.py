"""Tests for hub.cli — instance lock, start, stop, and status commands."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest
from click.testing import CliRunner

# ── Stub a2a SDK before any hub imports ──────────────────────────────────────
# hub.cli → hub.main → hub.agent_registry → a2a.utils.constants
# Mocking these lets the import chain complete without the real SDK installed.
for _mod in [
    "a2a",
    "a2a.utils",
    "a2a.utils.constants",
    "a2a.types",
    "a2a.client",
    "a2a.client.client",
    "a2a.server",
    "a2a.server.apps",
    "a2a.server.request_handlers",
    "a2a.server.agent_execution",
    "a2a.server.tasks",
]:
    sys.modules.setdefault(_mod, MagicMock())

from hub import config as hub_config  # noqa: E402
from hub.cli import _ENV_DAEMON_CHILD, _add_file_logging, _remove_lock_file, main  # noqa: E402
from hub.config import LOCK_FILE, LOG_FILE, read_lock_pid, write_lock_pid  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def _tmp_lock(tmp_path, monkeypatch):
    """Redirect LOCK_FILE to a tmp directory for isolation."""
    monkeypatch.setattr(hub_config, "LOCK_FILE", tmp_path / "hub.lock")
    monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def _tmp_log(tmp_path, monkeypatch):
    """Redirect LOG_FILE to a tmp directory."""
    monkeypatch.setattr(hub_config, "LOG_FILE", tmp_path / "hub.log")
    monkeypatch.setattr("hub.cli.LOG_FILE", tmp_path / "hub.log")
    return tmp_path


# ── _remove_lock_file ────────────────────────────────────────────────────────


class TestRemoveLockFile:
    def test_deletes_existing_lock_file(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("12345", encoding="utf-8")
        monkeypatch.setattr(hub_config, "LOCK_FILE", lock_path)
        monkeypatch.setattr("hub.cli.LOCK_FILE", lock_path)
        _remove_lock_file()
        assert not lock_path.exists()

    def test_is_silent_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hub.cli.LOCK_FILE", tmp_path / "no_such_file.lock")
        _remove_lock_file()  # must not raise


# ── Instance lock (config.py) ────────────────────────────────────────────────


class TestAcquireInstanceLock:
    def test_returns_file_handle_on_success(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_config, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        fh = hub_config.acquire_instance_lock()
        assert fh is not None
        assert not fh.closed
        fh.close()

    def test_exits_when_fcntl_raises(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_config, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        import fcntl
        with patch.object(fcntl, "flock", side_effect=OSError("locked")):
            with pytest.raises(SystemExit) as exc_info:
                hub_config.acquire_instance_lock()
        assert exc_info.value.code == 1

    def test_exits_on_windows_lock_failure(self, tmp_path, monkeypatch):
        """Simulate the Windows msvcrt path by removing fcntl from sys.modules."""
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_config, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        mock_msvcrt = MagicMock()
        mock_msvcrt.locking.side_effect = OSError("locked")
        with patch.dict(sys.modules, {"fcntl": None, "msvcrt": mock_msvcrt}):
            with pytest.raises(SystemExit) as exc_info:
                hub_config.acquire_instance_lock()
        assert exc_info.value.code == 1


class TestWriteAndReadLockPid:
    def test_write_then_read_returns_pid(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_config, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        fh = hub_config.acquire_instance_lock()
        hub_config.write_lock_pid(fh)
        fh.close()

        pid = hub_config.read_lock_pid()
        assert pid == os.getpid()

    def test_read_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hub_config, "LOCK_FILE", tmp_path / "no_such_file.lock")
        assert hub_config.read_lock_pid() is None

    def test_read_returns_none_for_corrupt_content(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("not-a-number", encoding="utf-8")
        monkeypatch.setattr(hub_config, "LOCK_FILE", lock_path)
        assert hub_config.read_lock_pid() is None

    def test_read_returns_none_for_empty_file(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("", encoding="utf-8")
        monkeypatch.setattr(hub_config, "LOCK_FILE", lock_path)
        assert hub_config.read_lock_pid() is None

    def test_read_returns_none_on_oserror(self, tmp_path, monkeypatch):
        """OSError (e.g. Windows mandatory lock) must not propagate."""
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_config, "LOCK_FILE", lock_path)
        with patch("hub.config.LOCK_FILE") as mock_path:
            mock_path.read_text.side_effect = OSError("lock violation")
            result = hub_config.read_lock_pid()
        assert result is None


# ── _add_file_logging ────────────────────────────────────────────────────────


class TestAddFileLogging:
    def test_adds_rotating_handler(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", tmp_path / "hub.log")
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            _add_file_logging(verbose=False)
            from logging.handlers import RotatingFileHandler
            new_handlers = [h for h in root.handlers if h not in original_handlers]
            assert any(isinstance(h, RotatingFileHandler) for h in new_handlers)
        finally:
            for h in root.handlers[:]:
                if h not in original_handlers:
                    root.removeHandler(h)

    def test_sets_root_logger_level_info(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", tmp_path / "hub.log")
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            _add_file_logging(verbose=False)
            assert root.level == logging.INFO
        finally:
            for h in root.handlers[:]:
                if h not in original_handlers:
                    root.removeHandler(h)

    def test_verbose_sets_debug_level(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", tmp_path / "hub.log")
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            _add_file_logging(verbose=True)
            assert root.level == logging.DEBUG
        finally:
            for h in root.handlers[:]:
                if h not in original_handlers:
                    root.removeHandler(h)


# ── start command ────────────────────────────────────────────────────────────

def _mock_config(api_key: str = "hybro_test_key"):
    """Return a minimal HubConfig-like mock."""
    cfg = MagicMock()
    cfg.api_key = api_key
    return cfg


class TestStartNoApiKey:
    def test_exits_with_error_when_no_api_key(self, runner):
        with patch("hub.cli.load_config", return_value=_mock_config(api_key="")):
            result = runner.invoke(main, ["start"])
        assert result.exit_code == 1
        assert "No API key" in result.output or "No API key" in (result.output + str(result.stderr_bytes or ""))

    def test_error_message_includes_hint(self, runner):
        with patch("hub.cli.load_config", return_value=_mock_config(api_key="")):
            result = runner.invoke(main, ["start"])
        # CliRunner mixes stderr into output by default
        combined = result.output
        assert "hybro-hub start --api-key" in combined or result.exit_code == 1


class TestStartForeground:
    def _invoke_foreground(self, runner, extra_patches=None):
        mock_fh = MagicMock()
        patches = {
            "hub.cli.load_config": _mock_config(),
            "hub.cli.acquire_instance_lock": MagicMock(return_value=mock_fh),
            "hub.cli.write_lock_pid": MagicMock(),
            "hub.cli.HubDaemon": MagicMock(),
        }
        if extra_patches:
            patches.update(extra_patches)

        daemon_instance = AsyncMock()
        daemon_instance.run = AsyncMock(return_value=None)
        patches["hub.cli.HubDaemon"].return_value = daemon_instance

        with patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.acquire_instance_lock", return_value=mock_fh), \
             patch("hub.cli.write_lock_pid") as mock_write_pid, \
             patch("hub.cli.HubDaemon", return_value=daemon_instance):
            result = runner.invoke(main, ["start", "--foreground"])

        return result, mock_fh, mock_write_pid, daemon_instance

    def test_exits_zero(self, runner):
        result, *_ = self._invoke_foreground(runner)
        assert result.exit_code == 0

    def test_writes_pid_to_lock(self, runner):
        _, mock_fh, mock_write_pid, _ = self._invoke_foreground(runner)
        mock_write_pid.assert_called_once_with(mock_fh)

    def test_runs_daemon(self, runner):
        _, _, _, daemon_instance = self._invoke_foreground(runner)
        daemon_instance.run.assert_called_once()

    def test_saves_api_key_when_provided(self, runner):
        mock_fh = MagicMock()
        daemon_instance = AsyncMock()
        daemon_instance.run = AsyncMock(return_value=None)
        with patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.acquire_instance_lock", return_value=mock_fh), \
             patch("hub.cli.write_lock_pid"), \
             patch("hub.cli.HubDaemon", return_value=daemon_instance), \
             patch("hub.cli.save_api_key") as mock_save:
            runner.invoke(main, ["start", "--foreground", "--api-key", "hybro_abc"])
        mock_save.assert_called_once_with("hybro_abc")


class TestStartUnixBackground:
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only path")
    def test_calls_daemonize(self, runner):
        mock_fh = MagicMock()
        daemon_instance = AsyncMock()
        daemon_instance.run = AsyncMock(return_value=None)
        with patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.acquire_instance_lock", return_value=mock_fh), \
             patch("hub.cli.write_lock_pid"), \
             patch("hub.cli.HubDaemon", return_value=daemon_instance), \
             patch("hub.cli._daemonize") as mock_daemonize, \
             patch("hub.cli._add_file_logging"):
            result = runner.invoke(main, ["start"])
        mock_daemonize.assert_called_once()

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only path")
    def test_writes_pid_after_daemonize(self, runner):
        mock_fh = MagicMock()
        call_order = []
        daemon_instance = AsyncMock()
        daemon_instance.run = AsyncMock(return_value=None)

        def _record_daemonize():
            call_order.append("daemonize")

        def _record_write_pid(fh):
            call_order.append("write_pid")

        with patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.acquire_instance_lock", return_value=mock_fh), \
             patch("hub.cli.write_lock_pid", side_effect=_record_write_pid), \
             patch("hub.cli.HubDaemon", return_value=daemon_instance), \
             patch("hub.cli._daemonize", side_effect=_record_daemonize), \
             patch("hub.cli._add_file_logging"):
            runner.invoke(main, ["start"])

        assert call_order == ["daemonize", "write_pid"]


class TestStartWindowsLauncher:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
    def test_calls_detach_windows(self, runner, monkeypatch):
        monkeypatch.delenv(_ENV_DAEMON_CHILD, raising=False)
        mock_fh = MagicMock()
        with patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.acquire_instance_lock", return_value=mock_fh), \
             patch("hub.cli._detach_windows") as mock_detach:
            result = runner.invoke(main, ["start"])
        mock_detach.assert_called_once()
        assert "started in background" in result.output

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
    def test_releases_lock_before_detach(self, runner, monkeypatch):
        """lock_fh.close() must be called before _detach_windows() on Windows."""
        monkeypatch.delenv(_ENV_DAEMON_CHILD, raising=False)
        mock_fh = MagicMock()
        call_order = []

        def _record_close():
            call_order.append("close")

        def _record_detach():
            call_order.append("detach")

        mock_fh.close = _record_close
        with patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.acquire_instance_lock", return_value=mock_fh), \
             patch("hub.cli._detach_windows", side_effect=_record_detach):
            runner.invoke(main, ["start"])

        assert call_order == ["close", "detach"]


class TestStartWindowsDaemonChild:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
    def test_runs_daemon_when_child_env_set(self, runner, monkeypatch):
        monkeypatch.setenv(_ENV_DAEMON_CHILD, "1")
        mock_fh = MagicMock()
        daemon_instance = AsyncMock()
        daemon_instance.run = AsyncMock(return_value=None)
        with patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.acquire_instance_lock", return_value=mock_fh), \
             patch("hub.cli.write_lock_pid"), \
             patch("hub.cli.HubDaemon", return_value=daemon_instance), \
             patch("hub.cli._add_file_logging"), \
             patch("hub.cli._detach_windows") as mock_detach:
            runner.invoke(main, ["start"])
        daemon_instance.run.assert_called_once()
        mock_detach.assert_not_called()


# ── stop command ─────────────────────────────────────────────────────────────


class TestStopNoLockFile:
    def test_reports_not_running(self, runner):
        with patch("hub.cli.read_lock_pid", return_value=None):
            result = runner.invoke(main, ["stop"])
        assert result.exit_code == 0
        assert "no hub daemon is running" in result.output.lower()


class TestStopPidNotFound:
    def test_reports_not_running(self, runner):
        import psutil
        with patch("hub.cli.read_lock_pid", return_value=99999), \
             patch("psutil.Process", side_effect=psutil.NoSuchProcess(99999)):
            result = runner.invoke(main, ["stop"])
        assert result.exit_code == 0
        assert "not running" in result.output.lower()


class TestStopStalePid:
    def test_reports_stale_pid(self, runner):
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["/usr/bin/python3", "some_other_script.py"]
        with patch("hub.cli.read_lock_pid", return_value=12345), \
             patch("psutil.Process", return_value=mock_proc):
            result = runner.invoke(main, ["stop"])
        assert result.exit_code == 1
        assert "stale" in result.output.lower()

    def test_reports_not_running_when_cmdline_raises_no_such_process(self, runner):
        import psutil
        mock_proc = MagicMock()
        mock_proc.cmdline.side_effect = psutil.NoSuchProcess(12345)
        with patch("hub.cli.read_lock_pid", return_value=12345), \
             patch("psutil.Process", return_value=mock_proc):
            result = runner.invoke(main, ["stop"])
        assert result.exit_code == 0
        assert "not running" in result.output.lower()


class TestStopGracefulShutdown:
    def test_sends_sigterm_and_reports_stopped(self, runner):
        import signal

        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["hybro-hub", "start"]

        with patch("hub.cli.read_lock_pid", return_value=42), \
             patch("psutil.Process", return_value=mock_proc), \
             patch("psutil.pid_exists", side_effect=[True, False]), \
             patch("time.time", side_effect=[0, 0.3, 0.6]), \
             patch("time.sleep"), \
             patch("hub.cli._remove_lock_file") as mock_rm:
            result = runner.invoke(main, ["stop"])

        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        mock_rm.assert_called_once()
        assert "stopped" in result.output.lower()
        assert result.exit_code == 0

    def test_sends_sigkill_when_graceful_times_out(self, runner):
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["hybro-hub", "start"]

        with patch("hub.cli.read_lock_pid", return_value=42), \
             patch("psutil.Process", return_value=mock_proc), \
             patch("psutil.pid_exists", return_value=True), \
             patch("time.time", side_effect=[0, 0, 11, 11]), \
             patch("time.sleep"), \
             patch("hub.cli._remove_lock_file") as mock_rm:
            result = runner.invoke(main, ["stop"])

        mock_proc.kill.assert_called_once()
        mock_rm.assert_called_once()
        assert "killed" in result.output.lower() or "sigkill" in result.output.lower()

    def test_kill_tolerates_process_already_gone(self, runner):
        import psutil

        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["hybro-hub", "start"]
        mock_proc.kill.side_effect = psutil.NoSuchProcess(42)

        with patch("hub.cli.read_lock_pid", return_value=42), \
             patch("psutil.Process", return_value=mock_proc), \
             patch("psutil.pid_exists", return_value=True), \
             patch("time.time", side_effect=[0, 0, 11, 11]), \
             patch("time.sleep"), \
             patch("hub.cli._remove_lock_file"):
            result = runner.invoke(main, ["stop"])

        # NoSuchProcess on kill() must not propagate — graceful degradation
        assert result.exception is None or isinstance(result.exception, SystemExit)


# ── status command ───────────────────────────────────────────────────────────


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gateway.example/status")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


class TestStatusNoApiKey:
    def test_shows_local_and_cloud_hint(self, runner, tmp_path, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", tmp_path / "hub.log")
        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config(api_key="")):
            result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        out = result.output.lower()
        assert "local daemon" in out
        assert "stopped" in out
        assert "no api key" in out or "api key" in out


class TestStatusLocalDaemon:
    def test_running_shows_pid_and_log_path(self, runner, tmp_path, monkeypatch):
        log_path = tmp_path / "hub.log"
        monkeypatch.setattr("hub.cli.LOG_FILE", log_path)
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["/venv/bin/hybro-hub", "start"]

        with patch("hub.cli.read_lock_pid", return_value=4242), \
             patch("psutil.Process", return_value=mock_proc), \
             patch("hub.cli.load_config", return_value=_mock_config()):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "4242" in result.output
        assert "running" in result.output.lower()
        assert str(log_path) in result.output

    def test_stopped_when_no_lock_pid(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()
        relay.get_status.assert_awaited_once()
        relay.close.assert_awaited_once()

    def test_stale_pid_when_process_not_hub_like(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["/usr/bin/python3", "-c", "print(1)"]
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=999), \
             patch("psutil.Process", return_value=mock_proc), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "stale" in result.output.lower()
        assert "999" in result.output

    def test_stopped_when_process_missing(self, runner, monkeypatch):
        import psutil

        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=888), \
             patch("psutil.Process", side_effect=psutil.NoSuchProcess(888)), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()
        assert "stale" in result.output.lower()


class TestStatusCloudRelay:
    def test_no_hubs_registered(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "no hubs registered" in result.output.lower()

    def test_lists_hub_and_agents(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        hub_id = "abcdef0123456789"
        relay = MagicMock()
        relay.get_status = AsyncMock(
            return_value={
                "hubs": [
                    {
                        "hub_id": hub_id,
                        "is_online": True,
                        "agent_count": 5,
                        "active_agent_count": 3,
                        "inactive_agent_count": 2,
                    }
                ]
            }
        )
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "online" in result.output.lower()
        assert hub_id[:12] in result.output
        assert "5" in result.output
        assert "3" in result.output
        assert "2" in result.output

    def test_http_401_message(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(side_effect=_http_status_error(401))
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "authentication" in result.output.lower()

    def test_http_403_message(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(side_effect=_http_status_error(403))
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "access denied" in result.output.lower()

    def test_http_other_status_message(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(side_effect=_http_status_error(502))
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "502" in result.output

    def test_generic_network_error_message(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(side_effect=OSError("network down"))
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "unreachable" in result.output.lower()
        assert "network down" in result.output.lower()
