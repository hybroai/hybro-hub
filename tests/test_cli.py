"""Tests for hybro-hub update, restart, and installer detection."""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from hub.cli import (
    _detect_installer_command,
    _CLI_ADAPTERS,
    _installer_display_name,
    _installed_version,
    _parse_version,
    _query_pypi_versions,
    _read_post_upgrade_version,
    _spawn_start,
    _stop_daemon,
    main,
)


@pytest.fixture(autouse=True)
def mock_a2a_adapter():
    """Mock a2a_adapter so agent_start() can be exercised without the package installed."""
    mock_serve = MagicMock()
    mock_load = MagicMock(return_value=MagicMock())

    mod_a2a = ModuleType("a2a_adapter")
    mod_a2a.serve_agent = mock_serve

    mod_loader = ModuleType("a2a_adapter.loader")
    mod_loader.load_adapter = mock_load

    saved = {}
    for name in ("a2a_adapter", "a2a_adapter.loader"):
        saved[name] = sys.modules.get(name)

    sys.modules["a2a_adapter"] = mod_a2a
    sys.modules["a2a_adapter.loader"] = mod_loader

    yield {"load_adapter": mock_load, "serve_agent": mock_serve}

    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


# ──── _parse_version ────


class TestParseVersion:
    def test_simple(self):
        assert _parse_version("0.1.16") == (0, 1, 16)

    def test_two_part(self):
        assert _parse_version("1.0") == (1, 0)

    def test_comparison(self):
        assert _parse_version("0.2.0") > _parse_version("0.1.99")

    def test_pre_release_raises(self):
        with pytest.raises(ValueError):
            _parse_version("0.2.0a1")


# ──── _installed_version ────


class TestInstalledVersion:
    def test_known_package(self):
        ver = _installed_version("click")
        assert ver is not None

    def test_missing_package(self):
        assert _installed_version("nonexistent-pkg-xyz-999") is None


# ──── _read_post_upgrade_version ────


class TestReadPostUpgradeVersion:
    def test_spawns_subprocess(self):
        with patch("hub.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1.2.3\n")
            result = _read_post_upgrade_version("hybro-hub")
            assert result == "1.2.3"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == sys.executable
            assert "-c" in args

    def test_returns_none_on_failure(self):
        with patch("hub.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _read_post_upgrade_version("hybro-hub") is None


# ──── _detect_installer_command ────


class TestDetectInstallerCommand:
    def test_pipx_path(self, tmp_path):
        fake_prefix = tmp_path / "pipx" / "venvs" / "hybro-hub"
        fake_prefix.mkdir(parents=True)
        with (
            patch("sys.prefix", str(fake_prefix)),
            patch("shutil.which", return_value="/usr/local/bin/pipx"),
            patch("importlib.metadata.distribution") as mock_dist,
        ):
            mock_dist.return_value.read_text.return_value = None
            cmd = _detect_installer_command()
        assert cmd == ["/usr/local/bin/pipx", "upgrade", "hybro-hub"]

    def test_uv_tool_path(self, tmp_path):
        fake_prefix = tmp_path / "uv" / "tools" / "hybro-hub"
        fake_prefix.mkdir(parents=True)
        with (
            patch("sys.prefix", str(fake_prefix)),
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch("importlib.metadata.distribution") as mock_dist,
        ):
            mock_dist.return_value.read_text.return_value = None
            cmd = _detect_installer_command()
        assert cmd == ["/usr/local/bin/uv", "tool", "upgrade", "hybro-hub"]

    def test_pip_fallback(self, tmp_path):
        fake_prefix = tmp_path / "some_venv"
        fake_prefix.mkdir(parents=True)
        with (
            patch("sys.prefix", str(fake_prefix)),
            patch("importlib.metadata.distribution") as mock_dist,
            patch("importlib.util.find_spec", return_value=True),
        ):
            mock_dist.return_value.read_text.return_value = None
            cmd = _detect_installer_command()
        assert cmd[0] == sys.executable
        assert "-m" in cmd and "pip" in cmd
        assert "hybro-hub" in cmd
        assert "a2a-adapter" in cmd
        assert "a2a-sdk" in cmd

    def test_uv_pip_fallback(self, tmp_path):
        fake_prefix = tmp_path / "some_venv"
        fake_prefix.mkdir(parents=True)
        with (
            patch("sys.prefix", str(fake_prefix)),
            patch("importlib.metadata.distribution") as mock_dist,
            patch("importlib.util.find_spec", return_value=None),
            patch("shutil.which", return_value="/usr/local/bin/uv"),
        ):
            mock_dist.return_value.read_text.return_value = None
            cmd = _detect_installer_command()
        assert cmd[0] == "/usr/local/bin/uv"
        assert "pip" in cmd
        assert "hybro-hub" in cmd

    def test_nothing_available(self, tmp_path):
        fake_prefix = tmp_path / "some_venv"
        fake_prefix.mkdir(parents=True)
        with (
            patch("sys.prefix", str(fake_prefix)),
            patch("importlib.metadata.distribution") as mock_dist,
            patch("importlib.util.find_spec", return_value=None),
            patch("shutil.which", return_value=None),
        ):
            mock_dist.return_value.read_text.return_value = None
            with pytest.raises(click.ClickException, match="Could not find"):
                _detect_installer_command()

    def test_conda_env(self, tmp_path):
        fake_prefix = tmp_path / "conda" / "envs" / "myenv"
        fake_prefix.mkdir(parents=True)
        with (
            patch("sys.prefix", str(fake_prefix)),
            patch.dict("os.environ", {"CONDA_PREFIX": str(fake_prefix)}),
            patch("importlib.metadata.distribution") as mock_dist,
        ):
            mock_dist.return_value.read_text.return_value = None
            with pytest.raises(click.ClickException, match="conda"):
                _detect_installer_command()

    def test_editable_install(self):
        editable_json = '{"url": "file:///home/user/hybro-hub", "dir_info": {"editable": true}}'
        with patch("importlib.metadata.distribution") as mock_dist:
            mock_dist.return_value.read_text.return_value = editable_json
            with pytest.raises(click.ClickException, match="editable"):
                _detect_installer_command()


# ──── _installer_display_name ────


class TestInstallerDisplayName:
    def test_pip(self):
        assert _installer_display_name([sys.executable, "-m", "pip", "install"]) == "pip"

    def test_pipx(self):
        assert _installer_display_name(["/usr/bin/pipx", "upgrade", "hybro-hub"]) == "pipx"

    def test_uv_tool(self):
        assert _installer_display_name(["/usr/bin/uv", "tool", "upgrade"]) == "uv tool"

    def test_uv_pip(self):
        assert _installer_display_name(["/usr/bin/uv", "pip", "install"]) == "uv"


# ──── _stop_daemon ────


class TestStopDaemon:
    def test_not_running(self):
        with patch("hub.cli.read_lock_pid", return_value=None):
            assert _stop_daemon() is False

    def test_stale_pid_no_process(self):
        import psutil

        with (
            patch("hub.cli.read_lock_pid", return_value=99999),
            patch("psutil.Process", side_effect=psutil.NoSuchProcess(99999)),
            patch("hub.cli._remove_lock_file") as mock_rm,
        ):
            assert _stop_daemon() is False
            mock_rm.assert_called_once_with(99999)

    def test_pid_different_process(self):
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["/usr/bin/nginx", "-g", "daemon off"]
        with (
            patch("hub.cli.read_lock_pid", return_value=12345),
            patch("psutil.Process", return_value=mock_proc),
        ):
            with pytest.raises(click.ClickException, match="different process"):
                _stop_daemon()

    def test_sigterm_succeeds(self):
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["python", "-m", "hub", "start"]
        with (
            patch("hub.cli.read_lock_pid", return_value=12345),
            patch("psutil.Process", return_value=mock_proc),
            patch("hub.cli._spinning_wait", return_value=True),
            patch("hub.cli._remove_lock_file") as mock_rm,
            patch("psutil.pid_exists", return_value=False),
        ):
            assert _stop_daemon() is True
            mock_proc.send_signal.assert_called_once()
            mock_rm.assert_called_once_with(12345)

    def test_sigterm_timeout_sigkill(self):
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["python", "-m", "hub", "start"]
        with (
            patch("hub.cli.read_lock_pid", return_value=12345),
            patch("psutil.Process", return_value=mock_proc),
            patch("hub.cli._spinning_wait", return_value=False),
            patch("hub.cli._remove_lock_file") as mock_rm,
        ):
            assert _stop_daemon() is True
            mock_proc.kill.assert_called_once()
            mock_rm.assert_called_once_with(12345)


# ──── _spawn_start ────


class TestSpawnStart:
    def test_background_success(self):
        with (
            patch("hub.cli.subprocess.run", return_value=MagicMock(returncode=0)),
            patch("hub.cli.read_lock_pid", return_value=12345),
            patch("time.sleep"),
        ):
            assert _spawn_start() is True

    def test_background_failure(self):
        with (
            patch("hub.cli.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            assert _spawn_start() is False

    def test_background_no_lock_after_start(self):
        with (
            patch("hub.cli.subprocess.run", return_value=MagicMock(returncode=0)),
            patch("hub.cli.read_lock_pid", return_value=None),
            patch("time.sleep"),
        ):
            assert _spawn_start() is False

    def test_foreground_flag(self):
        with patch("hub.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _spawn_start(foreground=True)
            assert result is True
            args = mock_run.call_args[0][0]
            assert "--foreground" in args


# ──── CLI integration: hybro-hub update ────


class TestUpdateCommand:
    def _run(self, args=None, **kwargs):
        runner = CliRunner()
        return runner.invoke(main, ["update"] + (args or []), catch_exceptions=False, **kwargs)

    def test_dry_run_shows_upgrades(self):
        with (
            patch("hub.cli._installed_version", side_effect=lambda p: {"hybro-hub": "0.1.16", "a2a-adapter": "0.2.5", "a2a-sdk": "0.3.25"}.get(p)),
            patch("hub.cli._query_pypi_versions", return_value={"hybro-hub": "0.1.18", "a2a-adapter": "0.2.9", "a2a-sdk": "1.0.1"}),
        ):
            result = self._run(["--dry-run"])
        assert result.exit_code == 0
        assert "\u2192" in result.output
        assert "0.1.18" in result.output

    def test_already_up_to_date(self):
        with (
            patch("hub.cli._installed_version", side_effect=lambda p: {"hybro-hub": "0.1.18", "a2a-adapter": "0.2.9", "a2a-sdk": "1.0.1"}.get(p)),
            patch("hub.cli._query_pypi_versions", return_value={"hybro-hub": "0.1.18", "a2a-adapter": "0.2.9", "a2a-sdk": "1.0.1"}),
        ):
            result = self._run()
        assert result.exit_code == 0
        assert "up to date" in result.output

    def test_pypi_unreachable_dry_run(self):
        with (
            patch("hub.cli._installed_version", return_value="0.1.16"),
            patch("hub.cli._query_pypi_versions", return_value={"hybro-hub": None, "a2a-adapter": None, "a2a-sdk": None}),
        ):
            result = self._run(["--dry-run"])
        assert result.exit_code == 0
        assert "offline" in result.output.lower() or "PyPI" in result.output

    def test_upgrade_subprocess_fails(self):
        with (
            patch("hub.cli._installed_version", side_effect=lambda p: "0.1.16"),
            patch("hub.cli._query_pypi_versions", return_value={"hybro-hub": "0.2.0", "a2a-adapter": "0.3.0", "a2a-sdk": "0.4.0"}),
            patch("hub.cli._detect_installer_command", return_value=[sys.executable, "-m", "pip", "install", "--upgrade", "hybro-hub"]),
            patch("hub.cli.subprocess.run", return_value=MagicMock(returncode=1, stderr="some error", stdout="")),
        ):
            result = self._run()
        assert result.exit_code == 1

    def test_permission_denied_suggests_sudo(self):
        with (
            patch("hub.cli._installed_version", side_effect=lambda p: "0.1.16"),
            patch("hub.cli._query_pypi_versions", return_value={"hybro-hub": "0.2.0", "a2a-adapter": "0.3.0", "a2a-sdk": "0.4.0"}),
            patch("hub.cli._detect_installer_command", return_value=[sys.executable, "-m", "pip", "install", "--upgrade", "hybro-hub"]),
            patch("hub.cli.subprocess.run", return_value=MagicMock(returncode=1, stderr="Permission denied", stdout="")),
        ):
            result = self._run()
        assert result.exit_code == 1
        assert "sudo" in result.output

    def test_restart_flag(self):
        with (
            patch("hub.cli._installed_version", side_effect=lambda p: "0.1.16"),
            patch("hub.cli._query_pypi_versions", return_value={"hybro-hub": "0.2.0", "a2a-adapter": "0.3.0", "a2a-sdk": "0.4.0"}),
            patch("hub.cli._detect_installer_command", return_value=[sys.executable, "-m", "pip", "install", "--upgrade", "hybro-hub"]),
            patch("hub.cli.subprocess.run", return_value=MagicMock(returncode=0, stderr="", stdout="")),
            patch("hub.cli._read_post_upgrade_version", return_value="0.2.0"),
            patch("hub.cli._stop_daemon", return_value=True) as mock_stop,
            patch("hub.cli._spawn_start", return_value=True) as mock_start,
        ):
            result = self._run(["--restart"])
        assert result.exit_code == 0
        mock_stop.assert_called_once()
        mock_start.assert_called_once()

    def test_shows_restart_hint_when_daemon_running(self):
        with (
            patch("hub.cli._installed_version", side_effect=lambda p: "0.1.16"),
            patch("hub.cli._query_pypi_versions", return_value={"hybro-hub": "0.2.0", "a2a-adapter": "0.3.0", "a2a-sdk": "0.4.0"}),
            patch("hub.cli._detect_installer_command", return_value=[sys.executable, "-m", "pip", "install", "--upgrade", "hybro-hub"]),
            patch("hub.cli.subprocess.run", return_value=MagicMock(returncode=0, stderr="", stdout="")),
            patch("hub.cli._read_post_upgrade_version", return_value="0.2.0"),
            patch("hub.cli.read_lock_pid", return_value=12345),
        ):
            result = self._run()
        assert result.exit_code == 0
        assert "hybro-hub restart" in result.output


# ──── CLI integration: hybro-hub restart ────


class TestRestartCommand:
    def _run(self, args=None):
        runner = CliRunner()
        return runner.invoke(main, ["restart"] + (args or []), catch_exceptions=False)

    def test_restart_running_daemon(self):
        with (
            patch("hub.cli._stop_daemon", return_value=True),
            patch("hub.cli._spawn_start", return_value=True) as mock_start,
        ):
            result = self._run()
        assert result.exit_code == 0
        mock_start.assert_called_once_with(foreground=False)

    def test_restart_when_not_running(self):
        with (
            patch("hub.cli._stop_daemon", return_value=False),
            patch("hub.cli._spawn_start", return_value=True),
        ):
            result = self._run()
        assert result.exit_code == 0
        assert "starting fresh" in result.output

    def test_restart_foreground(self):
        with (
            patch("hub.cli._stop_daemon", return_value=True),
            patch("hub.cli._spawn_start", return_value=True) as mock_start,
        ):
            result = self._run(["--foreground"])
        assert result.exit_code == 0
        mock_start.assert_called_once_with(foreground=True)

    def test_restart_pid_different_process(self):
        with patch("hub.cli._stop_daemon", side_effect=click.ClickException("different process")):
            result = self._run()
        assert result.exit_code != 0
        assert "different process" in result.output

    def test_restart_warns_if_start_fails(self):
        with (
            patch("hub.cli._stop_daemon", return_value=True),
            patch("hub.cli._spawn_start", return_value=False),
        ):
            result = self._run()
        assert result.exit_code == 0
        assert "may not have started" in result.output


# ──── CLI integration: hybro-hub --version ────


class TestVersionFlag:
    def test_version_output(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "hybro-hub" in result.output


# ──── CLI integration: hybro-hub agent start claude-code/codex ────


class TestCLIAdapters:
    def test_claude_code_registered(self):
        assert "claude-code" in _CLI_ADAPTERS

    def test_codex_registered(self):
        assert "codex" in _CLI_ADAPTERS

    def test_all_adapters_have_required_keys(self):
        for name, info in _CLI_ADAPTERS.items():
            assert "description" in info, f"{name} missing description"
            assert "install_hint" in info, f"{name} missing install_hint"


class TestAgentStartClaudeCode:
    def test_claude_code_default_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["agent", "start", "claude-code"])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["adapter"] == "claude-code"
        assert "working_dir" in config
        assert config["name"] == "Claude Code Agent"
        mock_a2a_adapter["serve_agent"].assert_called_once()

    def test_claude_code_explicit_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "start", "claude-code", "--working-dir", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["working_dir"] == str(tmp_path)

    def test_claude_code_custom_path(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "agent",
                "start",
                "claude-code",
                "--working-dir",
                str(tmp_path),
                "--claude-path",
                "/opt/bin/claude",
            ],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["claude_path"] == "/opt/bin/claude"

    def test_claude_code_custom_name_and_port(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "agent",
                "start",
                "claude-code",
                "--working-dir",
                str(tmp_path),
                "--name",
                "My Claude",
                "--port",
                "9010",
            ],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["name"] == "My Claude"
        _, kwargs = mock_a2a_adapter["serve_agent"].call_args
        assert kwargs["port"] == 9010

    def test_claude_code_with_timeout(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "agent",
                "start",
                "claude-code",
                "--working-dir",
                str(tmp_path),
                "--timeout",
                "300",
            ],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["timeout"] == 300

    def test_claude_code_output_shows_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "start", "claude-code", "--working-dir", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert "Working dir:" in result.output
        assert str(tmp_path) in result.output

    def test_claude_code_no_claude_path_in_config(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "start", "claude-code", "--working-dir", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert "claude_path" not in config


class TestAgentStartCodex:
    def test_codex_default_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["agent", "start", "codex"])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["adapter"] == "codex"
        assert config["name"] == "Codex Agent"
        mock_a2a_adapter["serve_agent"].assert_called_once()

    def test_codex_explicit_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "start", "codex", "--working-dir", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["working_dir"] == str(tmp_path)

    def test_codex_custom_path(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "agent",
                "start",
                "codex",
                "--working-dir",
                str(tmp_path),
                "--codex-path",
                "/opt/bin/codex",
            ],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["codex_path"] == "/opt/bin/codex"

    def test_codex_with_timeout(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "agent",
                "start",
                "codex",
                "--working-dir",
                str(tmp_path),
                "--timeout",
                "600",
            ],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["timeout"] == 600

    def test_codex_output_shows_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "start", "codex", "--working-dir", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert "Working dir:" in result.output

    def test_codex_no_codex_path_in_config(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "start", "codex", "--working-dir", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert "codex_path" not in config


class TestWorkingDirValidation:
    def test_claude_code_bad_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "agent",
                "start",
                "claude-code",
                "--working-dir",
                str(tmp_path / "nonexistent"),
            ],
        )
        assert result.exit_code != 0
        assert "Working directory does not exist" in result.output
        mock_a2a_adapter["load_adapter"].assert_not_called()

    def test_codex_bad_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "agent",
                "start",
                "codex",
                "--working-dir",
                str(tmp_path / "nonexistent"),
            ],
        )
        assert result.exit_code != 0
        assert "Working directory does not exist" in result.output
        mock_a2a_adapter["load_adapter"].assert_not_called()


class TestConfigWorkingDirHandling:
    def test_claude_code_config_defaults_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config_path = Path("claude-agent.yaml")
            config_path.write_text("adapter: claude-code\n", encoding="utf-8")
            cwd = os.getcwd()

            result = runner.invoke(main, ["agent", "start", "--config", str(config_path)])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["working_dir"] == cwd
        assert "Working dir:" in result.output

    def test_codex_config_defaults_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config_path = Path("codex-agent.yaml")
            config_path.write_text("adapter: codex\n", encoding="utf-8")
            cwd = os.getcwd()

            result = runner.invoke(main, ["agent", "start", "--config", str(config_path)])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["working_dir"] == cwd
        assert "Working dir:" in result.output

    def test_claude_code_config_rejects_bad_working_dir(
        self, tmp_path, mock_a2a_adapter,
    ):
        runner = CliRunner()
        config_path = tmp_path / "claude-agent.yaml"
        config_path.write_text(
            f"adapter: claude-code\nworking_dir: {tmp_path / 'missing'}\n",
            encoding="utf-8",
        )

        result = runner.invoke(main, ["agent", "start", "--config", str(config_path)])

        assert result.exit_code != 0
        assert "Working directory does not exist" in result.output
        mock_a2a_adapter["load_adapter"].assert_not_called()

    def test_codex_config_rejects_bad_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        config_path = tmp_path / "codex-agent.yaml"
        config_path.write_text(
            f"adapter: codex\nworking_dir: {tmp_path / 'missing'}\n",
            encoding="utf-8",
        )

        result = runner.invoke(main, ["agent", "start", "--config", str(config_path)])

        assert result.exit_code != 0
        assert "Working directory does not exist" in result.output
        mock_a2a_adapter["load_adapter"].assert_not_called()


class TestAdapterLoadingErrors:
    def test_import_error(self, tmp_path, mock_a2a_adapter):
        mock_a2a_adapter["load_adapter"].side_effect = ImportError("no module")
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "start", "claude-code", "--working-dir", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "Error" in result.output

    def test_value_error(self, tmp_path, mock_a2a_adapter):
        mock_a2a_adapter["load_adapter"].side_effect = ValueError("bad config")
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agent", "start", "codex", "--working-dir", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "Error" in result.output


class TestInvalidAdapter:
    def test_unknown_adapter_rejected(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "start", "unknown-adapter"])
        assert result.exit_code != 0
