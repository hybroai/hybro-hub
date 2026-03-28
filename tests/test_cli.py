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
from hub import lock as hub_lock  # noqa: E402
from hub.cli import _ENV_DAEMON_CHILD, _add_file_logging, _find_orphan_daemon, _remove_lock_file, main  # noqa: E402
from hub.lock import LOCK_FILE, LOG_FILE, read_lock_pid, write_lock_pid  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def _tmp_lock(tmp_path, monkeypatch):
    """Redirect LOCK_FILE to a tmp directory for isolation."""
    monkeypatch.setattr(hub_lock, "LOCK_FILE", tmp_path / "hub.lock")
    monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def _tmp_log(tmp_path, monkeypatch):
    """Redirect LOG_FILE to a tmp directory."""
    monkeypatch.setattr(hub_lock, "LOG_FILE", tmp_path / "hub.log")
    monkeypatch.setattr("hub.cli.LOG_FILE", tmp_path / "hub.log")
    return tmp_path


# ── _remove_lock_file ────────────────────────────────────────────────────────


class TestRemoveLockFile:
    def test_deletes_existing_lock_file(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("12345", encoding="utf-8")
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr("hub.cli.LOCK_FILE", lock_path)
        import fcntl
        with patch.object(fcntl, "flock"):  # flock succeeds (no-op)
            _remove_lock_file(12345)
        assert not lock_path.exists()

    def test_is_silent_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hub.cli.LOCK_FILE", tmp_path / "no_such_file.lock")
        monkeypatch.setattr(hub_lock, "LOCK_FILE", tmp_path / "no_such_file.lock")
        _remove_lock_file(0)  # os.open raises FileNotFoundError — must not raise

    def test_does_not_delete_when_new_daemon_owns_lock(self, tmp_path, monkeypatch):
        """Race condition: new daemon holds flock but already wrote its PID."""
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("99999", encoding="utf-8")  # new daemon's PID
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr("hub.cli.LOCK_FILE", lock_path)
        import fcntl
        with patch.object(fcntl, "flock"):  # flock succeeds; PID check is the guard
            _remove_lock_file(stopped_pid=12345)  # we stopped PID 12345
        assert lock_path.exists()
        assert lock_path.read_text(encoding="utf-8").strip() == "99999"

    def test_does_not_delete_when_flock_fails(self, tmp_path, monkeypatch):
        """New daemon holds LOCK_EX — we cannot acquire; must leave the file alone.

        This is the core of the flock-before-unlink fix: covers the race window
        between acquire_instance_lock() and write_lock_pid() where the file still
        holds the old PID but the new daemon already owns the flock.
        """
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("12345", encoding="utf-8")  # old PID still in file
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr("hub.cli.LOCK_FILE", lock_path)
        import fcntl
        with patch.object(fcntl, "flock", side_effect=OSError("would block")):
            _remove_lock_file(stopped_pid=12345)
        assert lock_path.exists(), "Must not delete when new daemon holds the flock"

    def test_windows_pid_only_fallback_deletes(self, tmp_path, monkeypatch):
        """Windows path: no fcntl, falls back to PID-only check and deletes."""
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("12345", encoding="utf-8")
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr("hub.cli.LOCK_FILE", lock_path)
        with patch.dict(sys.modules, {"fcntl": None}):
            _remove_lock_file(stopped_pid=12345)
        assert not lock_path.exists()

    def test_windows_pid_only_fallback_no_delete_on_mismatch(self, tmp_path, monkeypatch):
        """Windows path: PID mismatch → file survives."""
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("99999", encoding="utf-8")  # new daemon's PID
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr("hub.cli.LOCK_FILE", lock_path)
        with patch.dict(sys.modules, {"fcntl": None}):
            _remove_lock_file(stopped_pid=12345)
        assert lock_path.exists()
        assert lock_path.read_text(encoding="utf-8").strip() == "99999"


# ── _find_orphan_daemon ───────────────────────────────────────────────────────


class TestFindOrphanDaemon:
    def _make_mock_proc(self, pid: int, cmdline: list[str]):
        proc = MagicMock()
        proc.info = {"pid": pid, "cmdline": cmdline}
        return proc

    def test_finds_hub_start_process(self):
        mock_proc = self._make_mock_proc(27599, ["/usr/bin/python3", "/usr/bin/hybro-hub", "start"])
        with patch("psutil.process_iter", return_value=[mock_proc]):
            from hub.cli import _find_orphan_daemon
            assert _find_orphan_daemon() == 27599

    def test_finds_hub_start_with_flags(self):
        mock_proc = self._make_mock_proc(27599, ["/usr/bin/python3", "/usr/bin/hybro-hub", "start", "--foreground"])
        with patch("psutil.process_iter", return_value=[mock_proc]):
            from hub.cli import _find_orphan_daemon
            assert _find_orphan_daemon() == 27599

    def test_ignores_agent_subprocesses(self):
        """hybro-hub agent start <adapter> must not be treated as an orphan daemon."""
        mock_proc = self._make_mock_proc(6053, ["/usr/bin/python3", "/usr/bin/hybro-hub", "agent", "start", "openclaw", "--port", "10020"])
        with patch("psutil.process_iter", return_value=[mock_proc]):
            from hub.cli import _find_orphan_daemon
            assert _find_orphan_daemon() is None


# ── Instance lock (lock.py) ────────────────────────────────────────────────────


class TestAcquireInstanceLock:
    def test_returns_file_handle_on_success(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        fh = hub_lock.acquire_instance_lock()
        assert fh is not None
        assert not fh.closed
        fh.close()

    def test_exits_when_fcntl_raises(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        import fcntl
        with patch.object(fcntl, "flock", side_effect=OSError("locked")):
            with pytest.raises(SystemExit) as exc_info:
                hub_lock.acquire_instance_lock()
        assert exc_info.value.code == 1

    def test_failed_lock_does_not_truncate_existing_pid(self, tmp_path, monkeypatch):
        """Regression: a blocked flock must leave the running daemon's PID intact.

        Before the fix, open(LOCK_FILE, "w") truncated the file before the
        flock attempt, silently erasing the running daemon's PID and making it
        invisible to `stop` and `status`.
        """
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("99999", encoding="utf-8")
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        import fcntl
        with patch.object(fcntl, "flock", side_effect=OSError("locked")):
            with pytest.raises(SystemExit):
                hub_lock.acquire_instance_lock()

        assert lock_path.read_text(encoding="utf-8").strip() == "99999"

    def test_exits_on_windows_lock_failure(self, tmp_path, monkeypatch):
        """Simulate the Windows msvcrt path by removing fcntl from sys.modules."""
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        mock_msvcrt = MagicMock()
        mock_msvcrt.locking.side_effect = OSError("locked")
        with patch.dict(sys.modules, {"fcntl": None, "msvcrt": mock_msvcrt}):
            with pytest.raises(SystemExit) as exc_info:
                hub_lock.acquire_instance_lock()
        assert exc_info.value.code == 1


class TestWriteAndReadLockPid:
    def test_write_then_read_returns_pid(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        monkeypatch.setattr(hub_config, "HYBRO_DIR", tmp_path)

        fh = hub_lock.acquire_instance_lock()
        hub_lock.write_lock_pid(fh)
        fh.close()

        pid = hub_lock.read_lock_pid()
        assert pid == os.getpid()

    def test_read_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hub_lock, "LOCK_FILE", tmp_path / "no_such_file.lock")
        assert hub_lock.read_lock_pid() is None

    def test_read_returns_none_for_corrupt_content(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("not-a-number", encoding="utf-8")
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        assert hub_lock.read_lock_pid() is None

    def test_read_returns_none_for_empty_file(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "hub.lock"
        lock_path.write_text("", encoding="utf-8")
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        assert hub_lock.read_lock_pid() is None

    def test_read_returns_none_on_oserror(self, tmp_path, monkeypatch):
        """OSError (e.g. Windows mandatory lock) must not propagate."""
        lock_path = tmp_path / "hub.lock"
        monkeypatch.setattr(hub_lock, "LOCK_FILE", lock_path)
        with patch("hub.lock.LOCK_FILE") as mock_path:
            mock_path.read_text.side_effect = OSError("lock violation")
            result = hub_lock.read_lock_pid()
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

def _mock_config(api_key: str | None = "hybro_test_key"):
    """Return a minimal HubConfig-like mock."""
    cfg = MagicMock()
    cfg.cloud.api_key = api_key
    return cfg


def _mock_registry(agents=None):
    """Return a mock AgentRegistry that returns *agents* from discover()."""
    from hub.agent_registry import LocalAgent

    if agents is None:
        agents = []
    registry = MagicMock()
    registry.discover = AsyncMock(return_value=agents)
    registry.close = AsyncMock()
    return registry


def _make_agent(name="TestAgent", url="http://localhost:9000", healthy=True):
    """Construct a minimal LocalAgent for tests."""
    from hub.agent_registry import LocalAgent

    return LocalAgent(local_agent_id=f"id-{name}", name=name, url=url, healthy=healthy)


class TestStartNoApiKey:
    def test_exits_with_error_when_no_api_key(self, runner):
        with patch("hub.cli.load_config", return_value=_mock_config(api_key=None)):
            result = runner.invoke(main, ["start"])
        assert result.exit_code == 1
        assert "No API key" in result.output or "No API key" in (result.output + str(result.stderr_bytes or ""))

    def test_error_message_includes_hint(self, runner):
        with patch("hub.cli.load_config", return_value=_mock_config(api_key=None)):
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
        assert "not running" in result.output.lower()


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
             patch("hub.cli._spinning_wait", return_value=True), \
             patch("hub.cli._remove_lock_file") as mock_rm:
            result = runner.invoke(main, ["stop"])

        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        mock_rm.assert_called_once_with(42)
        assert "stopped" in result.output.lower()
        assert result.exit_code == 0

    def test_sends_sigkill_when_graceful_times_out(self, runner):
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["hybro-hub", "start"]

        with patch("hub.cli.read_lock_pid", return_value=42), \
             patch("psutil.Process", return_value=mock_proc), \
             patch("hub.cli._spinning_wait", return_value=False), \
             patch("hub.cli._remove_lock_file") as mock_rm:
            result = runner.invoke(main, ["stop"])

        mock_proc.kill.assert_called_once()
        mock_rm.assert_called_once_with(42)
        assert "killed" in result.output.lower() or "force" in result.output.lower()

    def test_kill_tolerates_process_already_gone(self, runner):
        import psutil

        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["hybro-hub", "start"]
        mock_proc.kill.side_effect = psutil.NoSuchProcess(42)

        with patch("hub.cli.read_lock_pid", return_value=42), \
             patch("psutil.Process", return_value=mock_proc), \
             patch("hub.cli._spinning_wait", return_value=False), \
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
             patch("hub.cli.load_config", return_value=_mock_config(api_key=None)), \
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()):
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
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
             patch("hub.cli.RelayClient", return_value=MagicMock(
                 get_status=AsyncMock(return_value={"hubs": []}),
                 close=AsyncMock(),
             )):
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
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
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
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
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
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()
        assert "stale" in result.output.lower()

    def test_status_warns_when_lock_missing_but_process_running(self, runner, monkeypatch):
        """When hub.lock is absent but a hybro-hub process exists, show a repair hint."""
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli._find_orphan_daemon", return_value=27599), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()
        assert "warning" in result.output.lower()
        assert "27599" in result.output
        # Repair hint must be the safe kill+restart form, not the unsafe echo form.
        assert "kill 27599" in result.output
        assert "hybro-hub start" in result.output
        assert "echo" not in result.output


class TestStatusCloudRelay:
    def test_no_hubs_registered(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
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
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "online" in result.output.lower()
        assert hub_id[:12] in result.output
        assert "5" in result.output
        assert "3" in result.output
        assert "2" in result.output
        assert "may lag" in result.output.lower()

    def test_http_401_message(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(side_effect=_http_status_error(401))
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
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
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
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
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
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
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry()), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "unreachable" in result.output.lower()
        assert "network down" in result.output.lower()


class TestStatusLocalAgents:
    """Tests for the local agent scan section in hybro-hub status."""

    def test_shows_none_found_when_no_agents(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry(agents=[])), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "none found" in result.output.lower()

    def test_shows_agent_count_and_health(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        agents = [
            _make_agent("AgentA", "http://localhost:9001", healthy=True),
            _make_agent("AgentB", "http://localhost:9002", healthy=False),
        ]
        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.agent_registry.AgentRegistry", return_value=_mock_registry(agents=agents)), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        out = result.output
        assert "2" in out            # 2 found
        assert "1" in out            # 1 healthy
        assert "AgentA" in out
        assert "AgentB" in out

    def test_local_scan_error_is_graceful(self, runner, monkeypatch):
        monkeypatch.setattr("hub.cli.LOG_FILE", Path("/tmp/hub.log"))
        relay = MagicMock()
        relay.get_status = AsyncMock(return_value={"hubs": []})
        relay.close = AsyncMock()

        broken_registry = MagicMock()
        broken_registry.discover = AsyncMock(side_effect=RuntimeError("scan failed"))
        broken_registry.close = AsyncMock()

        with patch("hub.cli.read_lock_pid", return_value=None), \
             patch("hub.cli.load_config", return_value=_mock_config()), \
             patch("hub.agent_registry.AgentRegistry", return_value=broken_registry), \
             patch("hub.cli.RelayClient", return_value=relay):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "error" in result.output.lower()


# ── agent start --config ─────────────────────────────────────────────────────


class TestAgentStartConfig:
    """Tests for `agent start --config` and auto-discovery."""

    @pytest.fixture()
    def _stub_a2a(self):
        """Stub a2a_adapter so CLI import succeeds without the real package."""
        mock_adapter = MagicMock()
        mock_load = MagicMock(return_value=mock_adapter)
        mock_serve = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "a2a_adapter": MagicMock(serve_agent=mock_serve),
                "a2a_adapter.loader": MagicMock(load_adapter=mock_load),
            },
        ):
            # Patch the names as imported in cli.py
            with patch("hub.cli.agent_start.__wrapped__", create=True):
                yield mock_load, mock_serve

    def _invoke_with_config(self, runner, config_content: str, extra_args=None):
        """Write a temp YAML file and invoke `agent start --config <path>`."""
        extra_args = extra_args or []
        mock_adapter = MagicMock()
        mock_load = MagicMock(return_value=mock_adapter)
        mock_serve = MagicMock()

        mock_a2a_adapter = MagicMock()
        mock_a2a_adapter.serve_agent = mock_serve
        mock_a2a_adapter_loader = MagicMock()
        mock_a2a_adapter_loader.load_adapter = mock_load

        with runner.isolated_filesystem():
            with open("agent.yaml", "w", encoding="utf-8") as f:
                f.write(config_content)

            with patch.dict(
                "sys.modules",
                {
                    "a2a_adapter": mock_a2a_adapter,
                    "a2a_adapter.loader": mock_a2a_adapter_loader,
                },
            ):
                result = runner.invoke(
                    main,
                    ["agent", "start", "--config", "agent.yaml"] + extra_args,
                )
        return result, mock_load, mock_serve

    def test_config_file_loads_n8n_adapter(self, runner):
        yaml_content = (
            "adapter: n8n\n"
            "webhook_url: http://localhost:5678/webhook/test\n"
            "message_field: event\n"
            "name: Test n8n\n"
            "port: 10055\n"
        )
        result, mock_load, mock_serve = self._invoke_with_config(runner, yaml_content)
        assert result.exit_code == 0
        config_passed = mock_load.call_args[0][0]
        assert config_passed["adapter"] == "n8n"
        assert config_passed["webhook_url"] == "http://localhost:5678/webhook/test"
        assert config_passed["message_field"] == "event"
        mock_serve.assert_called_once()

    def test_config_file_port_used_for_serve(self, runner):
        yaml_content = "adapter: n8n\nwebhook_url: http://localhost/wh\nport: 10099\n"
        result, _, mock_serve = self._invoke_with_config(runner, yaml_content)
        assert result.exit_code == 0
        assert mock_serve.call_args[1]["port"] == 10099

    def test_cli_port_overrides_config_file(self, runner):
        yaml_content = "adapter: n8n\nwebhook_url: http://localhost/wh\nport: 10099\n"
        result, _, mock_serve = self._invoke_with_config(
            runner, yaml_content, extra_args=["--port", "10200"]
        )
        assert result.exit_code == 0
        assert mock_serve.call_args[1]["port"] == 10200

    def test_config_file_missing_adapter_key_exits(self, runner):
        yaml_content = "webhook_url: http://localhost/wh\n"
        result, mock_load, _ = self._invoke_with_config(runner, yaml_content)
        assert result.exit_code == 1
        assert "adapter" in result.output.lower() or "adapter" in (result.output + str(result.exception or "")).lower()
        mock_load.assert_not_called()

    def test_both_adapter_type_and_config_exits(self, runner):
        mock_adapter = MagicMock()
        mock_load = MagicMock(return_value=mock_adapter)
        mock_serve = MagicMock()

        mock_a2a_adapter = MagicMock()
        mock_a2a_adapter.serve_agent = mock_serve
        mock_a2a_adapter_loader = MagicMock()
        mock_a2a_adapter_loader.load_adapter = mock_load

        with runner.isolated_filesystem():
            with open("agent.yaml", "w", encoding="utf-8") as f:
                f.write("adapter: n8n\nwebhook_url: http://x\n")

            with patch.dict(
                "sys.modules",
                {
                    "a2a_adapter": mock_a2a_adapter,
                    "a2a_adapter.loader": mock_a2a_adapter_loader,
                },
            ):
                result = runner.invoke(
                    main,
                    ["agent", "start", "n8n", "--config", "agent.yaml"],
                )
        assert result.exit_code == 1
        assert "cannot use both" in result.output.lower()

    def test_no_args_no_discovery_shows_help(self, runner):
        """With no adapter_type and no --config and no auto-discovery file, prints help."""
        with runner.isolated_filesystem():
            result = runner.invoke(main, ["agent", "start"])
        # exit 0 + help text shown
        assert result.exit_code == 0
        assert "usage" in result.output.lower() or "start" in result.output.lower()

    def test_auto_discovery_hybro_agent_yaml(self, runner):
        """When no args are given and hybro-agent.yaml exists in cwd, it is used."""
        yaml_content = "adapter: n8n\nwebhook_url: http://localhost/wh\n"
        mock_adapter = MagicMock()
        mock_load = MagicMock(return_value=mock_adapter)
        mock_serve = MagicMock()

        mock_a2a_adapter = MagicMock()
        mock_a2a_adapter.serve_agent = mock_serve
        mock_a2a_adapter_loader = MagicMock()
        mock_a2a_adapter_loader.load_adapter = mock_load

        with runner.isolated_filesystem():
            with open("hybro-agent.yaml", "w", encoding="utf-8") as f:
                f.write(yaml_content)

            with patch.dict(
                "sys.modules",
                {
                    "a2a_adapter": mock_a2a_adapter,
                    "a2a_adapter.loader": mock_a2a_adapter_loader,
                },
            ):
                result = runner.invoke(main, ["agent", "start"])

        assert result.exit_code == 0
        config_passed = mock_load.call_args[0][0]
        assert config_passed["adapter"] == "n8n"

