"""Hub CLI entry points."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from typing import Callable

import click
import httpx

from .config import (
    load_config,
    save_api_key,
)
from .lock import (
    LOCK_FILE,
    LOG_FILE,
    acquire_instance_lock,
    read_lock_pid,
    write_lock_pid,
)
from .main import HubDaemon
from .relay_client import RelayClient

_STOP_TIMEOUT = 10  # seconds to wait for graceful shutdown before SIGKILL
_ENV_DAEMON_CHILD = "HYBRO_HUB_DAEMON_CHILD"  # set to "1" in the detached child


def _remove_lock_file(stopped_pid: int) -> None:
    """Delete the lock file only when no live daemon holds it and its PID matches stopped_pid.

    Two-layer guard against the restart race:

    1. flock check: attempt LOCK_EX | LOCK_NB on the file. If that fails with
       OSError (EWOULDBLOCK), a live daemon holds LOCK_EX — leave the file alone.
       This closes the window between acquire_instance_lock() (flock acquired,
       line ~256) and write_lock_pid() (PID written, line ~286) where the file
       still holds the old PID but the new daemon already owns the flock.

    2. PID check: only unlink if the file still records stopped_pid. Belt-and-
       suspenders for the case where the flock race resolved after the PID was
       written.
    """
    try:
        fd = os.open(str(LOCK_FILE), os.O_RDWR)
    except FileNotFoundError:
        return

    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            # Windows: fcntl unavailable; fall back to PID-only check.
            # msvcrt locks are not inherited across CreateProcess, so the
            # flock race window does not exist on Windows.
            current_pid = read_lock_pid()
            if current_pid == stopped_pid:
                try:
                    LOCK_FILE.unlink()
                except FileNotFoundError:
                    pass
            return
        except OSError:
            # EWOULDBLOCK: a live daemon holds LOCK_EX. Leave the file alone.
            return

        # We hold LOCK_EX — no other daemon is running. Safe to inspect and
        # conditionally delete.
        current_pid = read_lock_pid()
        if current_pid == stopped_pid:
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


def _find_orphan_daemon() -> int | None:
    """Scan running processes for a hybro-hub daemon with no lock file.

    Returns the PID of the first matching process, or None.
    Used by `status` to surface a repair hint when hub.lock is missing.

    Matches only the hub daemon (`hybro-hub start [flags]`), not agent
    subprocesses (`hybro-hub agent start ...`) which share the same binary.
    """
    import psutil

    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info["cmdline"] or []
                for i, part in enumerate(cmdline):
                    if "hybro-hub" in part.lower() or part.lower() == "hub":
                        # Daemon: hybro-hub start [flags]
                        # Agent:  hybro-hub agent start ...
                        # Only match if the next argument is exactly "start".
                        if i + 1 < len(cmdline) and cmdline[i + 1] == "start":
                            return proc.info["pid"]
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    return None


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Suppress internal hub chatter unless --verbose is passed; one-shot CLI
    # commands (agents, status) already surface the same info via click.echo.
    if not verbose:
        logging.getLogger("hub").setLevel(logging.WARNING)


def _add_file_logging(verbose: bool) -> None:
    """Attach a rotating file handler to the root logger for daemon mode."""
    from logging.handlers import RotatingFileHandler

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger().setLevel(level)
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)


def _spinning_wait(
    message: str,
    check_fn: Callable[[], bool],
    interval: float = 0.25,
    timeout: float = _STOP_TIMEOUT,
) -> bool:
    """Blocking wait with animated dots on stderr. Returns True if check_fn became True."""
    import time

    frames = ["   ", ".  ", ".. ", "..."]
    i = 0
    deadline = time.time() + timeout
    is_tty = sys.stderr.isatty()

    while time.time() < deadline:
        if check_fn():
            if is_tty:
                click.echo("\r" + " " * (len(message) + 6) + "\r", nl=False, err=True)
            return True
        if is_tty:
            click.echo(f"\r{message}{frames[i % len(frames)]}", nl=False, err=True)
            i += 1
        time.sleep(interval)

    if is_tty:
        click.echo("\r" + " " * (len(message) + 6) + "\r", nl=False, err=True)
    return False


# ──── version / upgrade helpers ────

_TRACKED_PACKAGES = ("hybro-hub", "a2a-adapter", "a2a-sdk")


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a PEP 440 release segment (e.g. ``"0.1.16"``) into a comparable tuple."""
    return tuple(int(x) for x in v.split("."))


def _installed_version(pkg: str) -> str | None:
    import importlib.metadata

    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return None


def _read_post_upgrade_version(pkg: str) -> str | None:
    """Read installed version in a fresh process (avoids metadata cache)."""
    result = subprocess.run(
        [sys.executable, "-c",
         f"import importlib.metadata; print(importlib.metadata.version('{pkg}'))"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _query_pypi_versions(packages: tuple[str, ...]) -> dict[str, str | None]:
    """Query PyPI for the latest version of each package (concurrently)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch(pkg: str) -> tuple[str, str | None]:
        try:
            resp = httpx.get(f"https://pypi.org/pypi/{pkg}/json", timeout=10)
            resp.raise_for_status()
            return pkg, resp.json()["info"]["version"]
        except Exception:
            return pkg, None

    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=len(packages)) as pool:
        futures = {pool.submit(_fetch, pkg): pkg for pkg in packages}
        for f in as_completed(futures):
            pkg, ver = f.result()
            results[pkg] = ver
    return results


def _detect_installer_command() -> list[str]:
    """Detect how hybro-hub was installed and return the appropriate upgrade command."""
    import importlib.metadata
    import importlib.util
    import pathlib
    import shutil

    venv = pathlib.Path(sys.prefix)

    # Priority 0: Editable/dev install — refuse to upgrade
    try:
        direct_url = importlib.metadata.distribution("hybro-hub").read_text("direct_url.json")
        if direct_url and '"editable"' in direct_url:
            raise click.ClickException(
                "hybro-hub is installed in editable (development) mode.\n"
                "Update with: git pull && pip install -e ."
            )
    except importlib.metadata.PackageNotFoundError:
        pass

    # Priority 1: pipx — venv lives at <pipx_home>/venvs/hybro-hub
    if venv.name == "hybro-hub" and venv.parent.name == "venvs":
        pipx = shutil.which("pipx")
        if pipx:
            return [pipx, "upgrade", "hybro-hub"]

    # Priority 2: uv tool — venv lives at <data_dir>/uv/tools/hybro-hub
    if venv.name == "hybro-hub" and venv.parent.name == "tools" and "uv" in str(venv):
        uv = shutil.which("uv")
        if uv:
            return [uv, "tool", "upgrade", "hybro-hub"]

    # Priority 3: conda — refuse with guidance
    if os.environ.get("CONDA_PREFIX") and "conda" in sys.prefix.lower():
        raise click.ClickException(
            "Detected conda environment.\n"
            "Update with: conda update hybro-hub"
        )

    # Priority 4: pip (list all 3 packages so transitive deps get upgraded)
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--upgrade",
                "hybro-hub", "a2a-adapter", "a2a-sdk"]

    # Priority 5: uv as pip replacement
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--upgrade", "--python", sys.executable,
                "hybro-hub", "a2a-adapter", "a2a-sdk"]

    # Priority 6: nothing works
    raise click.ClickException(
        "Could not find pip, uv, or pipx to perform the upgrade.\n"
        "Upgrade manually:\n"
        "  pip install --upgrade hybro-hub\n"
        "  pipx upgrade hybro-hub\n"
        "  uv tool upgrade hybro-hub"
    )


def _installer_display_name(cmd: list[str]) -> str:
    """Derive a human-friendly installer name from a command list."""
    first = os.path.basename(cmd[0])
    if "pipx" in first:
        return "pipx"
    if "uv" in first:
        return "uv tool" if "tool" in cmd else "uv"
    return "pip"


# ──── daemon control helpers ────

def _stop_daemon() -> bool:
    """Stop the running daemon.

    Returns True if a daemon was stopped, False if none was running.
    Raises click.ClickException for unrecoverable states (e.g. PID belongs to
    another process).
    """
    import signal

    import psutil

    pid = read_lock_pid()
    if pid is None:
        return False

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        _remove_lock_file(pid)
        return False

    try:
        cmdline = proc.cmdline()
        is_hub = any("hybro" in part.lower() or "hub" in part.lower() for part in cmdline)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        _remove_lock_file(pid)
        return False

    if not is_hub:
        raise click.ClickException(
            f"PID {pid} belongs to a different process — lock file is stale.\n"
            f"Run: rm {LOCK_FILE}"
        )

    if not sys.stderr.isatty():
        click.echo(f"Stopping hub daemon (PID {pid})...")
    proc.send_signal(signal.SIGTERM)

    stopped = _spinning_wait(
        f"Stopping hub daemon (PID {pid})",
        check_fn=lambda: not psutil.pid_exists(pid),
        interval=0.25,
        timeout=_STOP_TIMEOUT,
    )

    if stopped:
        _remove_lock_file(pid)
        click.echo("Hub daemon stopped.")
        return True

    click.echo(f"Daemon did not stop after {_STOP_TIMEOUT}s — force killing.")
    try:
        proc.kill()
    except psutil.NoSuchProcess:
        pass
    _remove_lock_file(pid)
    click.echo("Hub daemon killed.")
    return True


def _spawn_start(foreground: bool = False) -> bool:
    """Spawn ``hybro-hub start`` as a subprocess.

    Returns True if the daemon appears to have started successfully.
    stdout/stderr are inherited so the user sees output directly.
    """
    import time

    cmd = [sys.executable, "-m", "hub", "start"]
    if foreground:
        cmd.append("--foreground")
        subprocess.run(cmd)
        return True

    result = subprocess.run(cmd)
    if result.returncode != 0:
        return False

    time.sleep(0.5)
    return read_lock_pid() is not None


def _detach_windows() -> None:
    """Re-launch the current command as a detached background process (Windows only).

    Passes HYBRO_HUB_DAEMON_CHILD=1 in the environment so the child knows it
    should run as the daemon instead of spawning yet another child.
    stdout/stderr of the child are redirected to the log file.
    """
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env[_ENV_DAEMON_CHILD] = "1"

    log_f = open(LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115
    try:
        subprocess.Popen(
            sys.argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        )
    finally:
        log_f.close()



def _daemonize() -> None:
    """Double-fork daemonize (Unix only).

    Returns in the final daemon child process.  All intermediate parents exit
    via os._exit() to skip atexit/finalizer side-effects.
    The open lock file handle is inherited by the child so the flock stays held.
    """
    # First fork — detaches from the parent's process group
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    os.setsid()  # become session leader; detach from controlling terminal

    # Second fork — ensures the daemon can never re-acquire a terminal
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    os.chdir("/")  # don't hold any mount point
    os.umask(0o022)

    # Redirect stdin to /dev/null; stdout/stderr go to the log file so that
    # any stray print() calls or C-level output also ends up in the log.
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull_fd, sys.stdin.fileno())
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(devnull_fd)
    os.close(log_fd)


@click.group()
@click.version_option(
    version=None,
    package_name="hybro-hub",
    prog_name="hybro-hub",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Hybro Hub — bridge local A2A agents to hybro.ai."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# ──── hybro-hub start ────

@main.command()
@click.option("--api-key", default=None, help="Hybro API key (also saves to config).")
@click.option(
    "--foreground", "-f", is_flag=True,
    help="Run in the foreground instead of as a background daemon.",
)
@click.pass_context
def start(ctx: click.Context, api_key: str | None, foreground: bool) -> None:
    """Start the hub daemon (background by default).

    Logs are written to ~/.hybro/hub.log.
    Use --foreground / -f to keep the process attached to the terminal.
    """
    verbose: bool = ctx.obj.get("verbose", False)

    # Restore hub.* logging to INFO for the daemon — _setup_logging silences it
    # by default so that one-shot CLI commands (agents, status) stay clean.
    logging.getLogger("hub").setLevel(logging.DEBUG if verbose else logging.INFO)

    if api_key:
        save_api_key(api_key)

    config = load_config(api_key=api_key)
    if config.cloud.api_key is None:
        click.echo(
            "Error: No API key configured.\n"
            "Run: hybro-hub start --api-key hybro_...\n"
            "Or set HYBRO_API_KEY environment variable.",
            err=True,
        )
        sys.exit(1)

    # Acquire the instance lock before forking so the parent can report errors.
    lock_fh = acquire_instance_lock()

    if foreground:
        write_lock_pid(lock_fh)
        daemon = HubDaemon(config)
        asyncio.run(daemon.run())
        return

    # ── background daemon path ──

    if sys.platform == "win32":
        if os.environ.get(_ENV_DAEMON_CHILD) == "1":
            # We are the detached child — run the daemon.
            write_lock_pid(lock_fh)
            logging.getLogger().handlers.clear()
            _add_file_logging(verbose)
            daemon = HubDaemon(config)
            asyncio.run(daemon.run())
        else:
            # We are the launcher — spawn a detached child and exit.
            lock_fh.close()  # release before spawning so child can re-acquire
            _detach_windows()
            click.echo(f"Hub daemon started in background. Logs: {LOG_FILE}")
        return

    # ── Unix: double-fork ──
    click.echo(f"Hub daemon starting in background. Logs: {LOG_FILE}")
    _daemonize()

    # We are now the daemon child.
    write_lock_pid(lock_fh)

    # Re-configure logging: basicConfig was already called in the parent but
    # stdout is now the log file; add a proper RotatingFileHandler instead.
    logging.getLogger().handlers.clear()
    _add_file_logging(verbose)

    daemon = HubDaemon(config)
    asyncio.run(daemon.run())

# ──── hybro-hub stop ────

@main.command()
def stop() -> None:
    """Stop the running hub daemon."""
    try:
        was_running = _stop_daemon()
    except click.ClickException:
        raise
    if not was_running:
        click.echo("Hub daemon is not running.")


# ──── hybro-hub restart ────

@main.command()
@click.option(
    "--foreground", "-f", is_flag=True,
    help="After stopping, restart in the foreground (attached to terminal).",
)
def restart(foreground: bool) -> None:
    """Restart the hub daemon (stop + start)."""
    was_running = _stop_daemon()
    if not was_running:
        click.echo("Hub daemon is not running — starting fresh.")
    ok = _spawn_start(foreground=foreground)
    if not ok:
        click.echo(
            "Warning: daemon may not have started. Check: hybro-hub status",
            err=True,
        )


# ──── hybro-hub update ────

@main.command()
@click.option("--dry-run", is_flag=True, help="Check for available upgrades without installing.")
@click.option("--restart", "do_restart", is_flag=True,
              help="After upgrade, restart the daemon with the new code.")
def update(dry_run: bool, do_restart: bool) -> None:
    """Check for and install upgrades to hybro-hub and its dependencies."""
    # 1. Read installed versions
    installed = {pkg: _installed_version(pkg) for pkg in _TRACKED_PACKAGES}

    click.echo("Current versions:")
    for pkg, ver in installed.items():
        click.echo(f"  {pkg:14s} {ver or 'not installed'}")
    click.echo()

    # 2. Query PyPI for latest versions
    latest = _query_pypi_versions(_TRACKED_PACKAGES)
    pypi_reachable = any(v is not None for v in latest.values())

    if not pypi_reachable:
        click.echo("Warning: could not reach PyPI to check latest versions.", err=True)
        if dry_run:
            click.echo("Cannot determine available upgrades offline.")
            return
        click.echo("Attempting upgrade anyway...\n")
    else:
        # 3. Compare
        upgrades: dict[str, tuple[str, str]] = {}
        for pkg in _TRACKED_PACKAGES:
            cur = installed.get(pkg)
            lat = latest.get(pkg)
            if cur and lat:
                try:
                    if _parse_version(lat) > _parse_version(cur):
                        upgrades[pkg] = (cur, lat)
                except ValueError:
                    pass

        if not upgrades:
            click.echo("Everything is up to date.")
            return

        click.echo("Available upgrades:")
        for pkg, (cur, lat) in upgrades.items():
            click.echo(f"  {pkg:14s} {cur} \u2192 {lat}")
        click.echo()

        if dry_run:
            return

    # 5. Detect installer
    cmd = _detect_installer_command()
    click.echo(f"Upgrading with {_installer_display_name(cmd)}...")

    # 6. Run upgrade subprocess
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        click.echo(f"Upgrade failed (exit code {result.returncode}).", err=True)
        if result.stderr:
            click.echo(result.stderr.rstrip(), err=True)
        if "Permission denied" in (result.stderr or ""):
            click.echo("\nTry: sudo hybro-hub update", err=True)
        else:
            click.echo(f"\nManual upgrade: {' '.join(cmd)}", err=True)
        sys.exit(1)

    # 8. Print diff
    click.echo("\u2713 Upgrade complete.\n")
    for pkg in _TRACKED_PACKAGES:
        old = installed.get(pkg) or "?"
        new = _read_post_upgrade_version(pkg) or "?"
        if old != new:
            click.echo(f"  {pkg:14s} {new} (was {old})")
        else:
            click.echo(f"  {pkg:14s} {new} (unchanged)")
    click.echo()

    # 9-10. Restart handling
    if do_restart:
        was_running = _stop_daemon()
        if not was_running:
            click.echo("Hub daemon was not running — starting fresh.")
        ok = _spawn_start()
        if not ok:
            click.echo(
                "Warning: daemon may not have started. Check: hybro-hub status",
                err=True,
            )
    elif read_lock_pid() is not None:
        click.echo("The hub daemon is running. Restart to use the new version:")
        click.echo("  hybro-hub restart")


# ──── hybro-hub status ────

@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show local daemon state and cloud relay status."""
    import psutil

    # ── Local daemon section ──────────────────────────────────────────────────
    pid = read_lock_pid()
    daemon_running = False
    if pid is not None:
        try:
            proc = psutil.Process(pid)
            cmdline = proc.cmdline()
            daemon_running = any(
                "hybro" in part.lower() or "hub" in part.lower()
                for part in cmdline
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            daemon_running = False

    if daemon_running:
        click.echo(f"  ✓  Local daemon   Running (PID {pid})")
        click.echo(f"     Log file:      {LOG_FILE}")
    else:
        click.echo("  ✗  Local daemon   Stopped")
        if pid is not None:
            click.echo(f"     (stale PID {pid} — daemon may have crashed)")
        elif (orphan_pid := _find_orphan_daemon()) is not None:
            click.echo(f"     (warning: process PID {orphan_pid} looks like hybro-hub but {LOCK_FILE.name} is missing)")
            click.echo(f"     Fix: kill {orphan_pid} && hybro-hub start")

    click.echo("")

    # ── Local agents section ──────────────────────────────────────────────────
    config = load_config()

    async def _full_status() -> None:
        from .agent_registry import AgentRegistry

        stop_event = asyncio.Event()

        async def _animate() -> None:
            frames = ["   ", ".  ", ".. ", "..."]
            i = 0
            while True:
                click.echo(
                    f"\r  Checking status{frames[i % len(frames)]}",
                    nl=False,
                    err=True,
                )
                i += 1
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.4)
                    break
                except asyncio.TimeoutError:
                    pass
            click.echo("\r" + " " * 40 + "\r", nl=False, err=True)

        if sys.stderr.isatty():
            anim_task = asyncio.create_task(_animate())
        else:
            anim_task = None

        # Run local discovery and cloud call concurrently.
        registry = AgentRegistry(config)

        async def _local_scan() -> list:
            try:
                return await registry.discover()
            finally:
                await registry.close()

        async def _cloud_call() -> dict | None:
            if config.cloud.api_key is None:
                return None
            relay = RelayClient(
                gateway_url=config.cloud.gateway_url,
                hub_id=config.hub_id,
                api_key=config.cloud.api_key or "",
            )
            try:
                return await relay.get_status()
            finally:
                await relay.close()

        local_agents, cloud_data = await asyncio.gather(
            _local_scan(),
            _cloud_call(),
            return_exceptions=True,
        )

        if anim_task is not None:
            stop_event.set()
            await anim_task

        # ── Local agents output ───────────────────────────────────────────────
        if isinstance(local_agents, Exception):
            click.echo("  ✗  Local agents   Error scanning local agents")
        else:
            healthy = [a for a in local_agents if a.healthy]
            n = len(local_agents)
            h = len(healthy)
            if n == 0:
                click.echo("  –  Local agents   None found")
            else:
                click.echo(f"  ✓  Local agents   {n} found  {h} healthy  {n - h} unhealthy")
                for a in local_agents:
                    symbol = "✓" if a.healthy else "✗"
                    click.echo(f"     {symbol}  {a.name}  ({a.url})")

        click.echo("")

        # ── Cloud relay output ────────────────────────────────────────────────
        if config.cloud.api_key is None:
            click.echo("     Cloud relay:   No API key — run: hybro-hub start --api-key hybro_...")
            return

        if isinstance(cloud_data, Exception):
            exc = cloud_data
            if isinstance(exc, httpx.HTTPStatusError):
                if exc.response.status_code == 401:
                    click.echo("  ✗  Cloud relay    Authentication failed — check your API key.")
                elif exc.response.status_code == 403:
                    click.echo("  ✗  Cloud relay    Access denied.")
                else:
                    click.echo(f"  ✗  Cloud relay    Error {exc.response.status_code} from server.")
            else:
                click.echo(f"  ✗  Cloud relay    Unreachable ({exc})")
            return

        hubs = cloud_data.get("hubs", []) if cloud_data else []
        if not hubs:
            click.echo("  ✗  Cloud relay    No hubs registered.")
            return
        for h in hubs:
            online = h.get("is_online")
            symbol = "✓" if online else "✗"
            state = "Online" if online else "Offline"
            total = h.get("agent_count", 0)
            active = h.get("active_agent_count", 0)
            inactive = h.get("inactive_agent_count", 0)
            click.echo(f"  {symbol}  Cloud relay    {state} (hub {h['hub_id'][:12]}...)")
            click.echo(
                f"     Agents:        {total} total  {active} active  {inactive} inactive"
                "  (cloud view, may lag)"
            )

    asyncio.run(_full_status())


# ──── hybro-hub agents ────

@main.command()
@click.pass_context
def agents(ctx: click.Context) -> None:
    """List discovered local agents."""
    from .agent_registry import AgentRegistry

    config = load_config()

    async def _agents() -> None:
        stop_event = asyncio.Event()

        async def _animate() -> None:
            frames = ["   ", ".  ", ".. ", "..."]
            i = 0
            while True:
                click.echo(
                    f"\rScanning for local A2A agents{frames[i % len(frames)]}",
                    nl=False,
                    err=True,
                )
                i += 1
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.4)
                    break
                except asyncio.TimeoutError:
                    pass
            # Erase the scanning line
            click.echo("\r" + " " * 50 + "\r", nl=False, err=True)

        if sys.stderr.isatty():
            anim_task = asyncio.create_task(_animate())
        else:
            click.echo("Scanning for local A2A agents...", err=True)
            anim_task = None

        registry = AgentRegistry(config)
        found = await registry.discover()
        await registry.close()

        if anim_task is not None:
            stop_event.set()
            await anim_task

        if not found:
            click.echo("No local agents found.")
            return
        n = len(found)
        click.echo(f"Found {n} agent{'s' if n != 1 else ''}:\n")
        for a in found:
            symbol = "✓" if a.healthy else "✗"
            click.echo(f"  {symbol}  {a.name}")
            click.echo(f"     URL: {a.url}")
            click.echo(f"     ID:  {a.local_agent_id}")

    asyncio.run(_agents())


# ──── hybro-hub agent start ────

@main.group()
def agent() -> None:
    """Manage local agent adapters."""


_CLI_ADAPTERS = {
    "ollama": {
        "description": "Local LLM via Ollama",
        "install_hint": "pip install a2a-adapter",
    },
    "openclaw": {
        "description": "OpenClaw AI agent via CLI subprocess",
        "install_hint": "pip install a2a-adapter",
    },
    "n8n": {
        "description": "n8n workflow via webhook",
        "install_hint": "pip install a2a-adapter",
    },
    "hermes": {
        "description": "Hermes Agent (tools, memory) via gateway pattern",
        "install_hint": "pip install a2a-adapter",
    },
}


def _validate_ollama_model(model: str, base_url: str = "http://localhost:11434") -> None:
    """Check if the specified model exists in Ollama.
    
    Raises click.ClickException if Ollama is not running or model not found.
    """
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        available_models = [m.get("name", "") for m in data.get("models", [])]
        
        # Check exact match or match without tag (e.g., "llama3.2" matches "llama3.2:latest")
        if ":" in model:
            # User specified exact tag - require exact match
            found = model in available_models
        else:
            # User specified base name only - match any variant
            found = any(
                m == model or m.startswith(f"{model}:")
                for m in available_models
            )
        
        if not found:
            if available_models:
                models_list = ", ".join(available_models[:5])
                if len(available_models) > 5:
                    models_list += f", ... ({len(available_models)} total)"
                raise click.ClickException(
                    f"Model '{model}' not found in Ollama.\n"
                    f"Available models: {models_list}\n"
                    f"Pull it with: ollama pull {model}"
                )
            else:
                raise click.ClickException(
                    f"Model '{model}' not found. No models available in Ollama.\n"
                    f"Pull it with: ollama pull {model}"
                )
    except httpx.ConnectError:
        raise click.ClickException(
            f"Cannot connect to Ollama at {base_url}.\n"
            "Is Ollama running? Start it with: ollama serve"
        )
    except httpx.HTTPStatusError as e:
        raise click.ClickException(f"Ollama API error: {e.response.status_code}")


@agent.command("start")
@click.argument("adapter_type", required=False,
    type=click.Choice(sorted(_CLI_ADAPTERS), case_sensitive=False))
@click.option("--config", "config_path", default=None, type=click.Path(exists=True),
    help="Path to a YAML agent config file. Mutually exclusive with adapter_type.")
@click.option("--port", default=10010, type=int, help="Port for the A2A server.")
@click.option("--name", "agent_name", default=None, help="Agent display name.")
@click.option("--model", default=None, help="[ollama] Model name (default: llama3.2).")
@click.option("--system-prompt", default=None, help="[ollama] System prompt.")
@click.option("--thinking", default=None, help="[openclaw] Thinking level (off/minimal/low/medium/high/xhigh).")
@click.option("--agent-id", default=None, help="[openclaw] OpenClaw agent ID.")
@click.option("--openclaw-path", default=None, help="[openclaw] Path to openclaw binary.")
@click.option("--webhook-url", default=None, help="[n8n] Webhook URL (required for n8n).")
@click.option("--provider", default=None, help="[hermes] LLM provider override (optional).")
@click.option(
    "--enabled-toolsets",
    "enabled_toolsets",
    default=None,
    help="[hermes] Comma-separated toolsets (default: hermes-cli).",
)
@click.option("--timeout", default=None, type=int, help="Request timeout in seconds.")
@click.pass_context
def agent_start(
    ctx: click.Context,
    adapter_type: str | None,
    config_path: str | None,
    port: int,
    agent_name: str | None,
    model: str | None,
    system_prompt: str | None,
    thinking: str | None,
    agent_id: str | None,
    openclaw_path: str | None,
    webhook_url: str | None,
    provider: str | None,
    enabled_toolsets: str | None,
    timeout: int | None,
) -> None:
    """Start a local A2A agent adapter.

    Supported adapters: ollama, openclaw, n8n, hermes.

    \b
    CLI flags are a convenience shortcut for common parameters.
    For full adapter control (e.g. n8n message_field, custom headers),
    use --config with a YAML file instead.

    \b
    Examples:
      hybro-hub agent start ollama
      hybro-hub agent start ollama --model mistral:7b --port 10020
      hybro-hub agent start openclaw --thinking medium
      hybro-hub agent start n8n --webhook-url http://localhost:5678/webhook/agent
      hybro-hub agent start hermes
      hybro-hub agent start hermes --model anthropic/claude-sonnet-4
      hybro-hub agent start --config hybro-agent.yaml
    """
    # Mutual-exclusion guard and auto-discovery
    if config_path and adapter_type:
        click.echo("Error: Cannot use both --config and an adapter type argument.", err=True)
        sys.exit(1)

    if not config_path and not adapter_type:
        for candidate in ("hybro-agent.yaml", ".hybro-agent.yaml"):
            if os.path.exists(candidate):
                config_path = candidate
                break
        else:
            click.echo(ctx.get_help())
            sys.exit(0)

    config: dict
    adapter_type_display: str

    if config_path:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if not isinstance(config, dict) or not config.get("adapter"):
            click.echo(
                f"Error: Config file missing required key: 'adapter'\n  File: {config_path}",
                err=True,
            )
            sys.exit(1)
        # CLI flags override config file values.
        # Use ParameterSource to detect flags the user explicitly provided,
        # rather than comparing against hard-coded default values.
        if ctx.get_parameter_source("port") == click.core.ParameterSource.COMMANDLINE:
            config["port"] = port
        if agent_name:
            config["name"] = agent_name
        if timeout:
            config["timeout"] = timeout
        adapter_type_display = config["adapter"]
        effective_port = config.get("port", port)
    else:
        # adapter_type is guaranteed non-None here
        config = {"adapter": adapter_type}
        adapter_type_display = adapter_type  # type: ignore[assignment]
        effective_port = port

        if adapter_type == "ollama":
            config["model"] = model or "llama3.2"
            _validate_ollama_model(config["model"])
            config["name"] = agent_name or f"Ollama ({config['model']})"
            config["description"] = f"Local LLM via Ollama ({config['model']})"
            if system_prompt:
                config["system_prompt"] = system_prompt
            if timeout:
                config["timeout"] = timeout

        elif adapter_type == "openclaw":
            config["name"] = agent_name or "OpenClaw Agent"
            config["description"] = "OpenClaw AI agent"
            if thinking:
                config["thinking"] = thinking
            if agent_id:
                config["agent_id"] = agent_id
            if openclaw_path:
                config["openclaw_path"] = openclaw_path
            if timeout:
                config["timeout"] = timeout

        elif adapter_type == "n8n":
            if not webhook_url:
                click.echo("Error: --webhook-url is required for n8n adapter.", err=True)
                sys.exit(1)
            config["webhook_url"] = webhook_url
            config["name"] = agent_name or "n8n Workflow Agent"
            config["description"] = "n8n workflow agent"
            if timeout:
                config["timeout"] = timeout

        elif adapter_type == "hermes":
            config["name"] = agent_name or "Hermes Agent"
            config["description"] = (
                "Hermes AI agent — tool use, persistent memory, streaming"
            )
            if model:
                config["model"] = model
            if provider:
                config["provider"] = provider
            if enabled_toolsets:
                ts = [s.strip() for s in enabled_toolsets.split(",") if s.strip()]
                config["enabled_toolsets"] = ts if ts else ["hermes-cli"]
            else:
                config["enabled_toolsets"] = ["hermes-cli"]
            if timeout:
                config["timeout"] = timeout

    try:
        from a2a_adapter import serve_agent
        from a2a_adapter.loader import load_adapter
    except ImportError:
        install_hint = _CLI_ADAPTERS.get(adapter_type_display, {}).get(
            "install_hint", "pip install a2a-adapter"
        )
        click.echo(
            f"Error: a2a-adapter package not installed.\nInstall with: {install_hint}",
            err=True,
        )
        sys.exit(1)

    # "port" is hub-only — strip it before passing config to the adapter loader.
    try:
        adapter = load_adapter({k: v for k, v in config.items() if k != "port"})
    except ImportError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"\nStarting {adapter_type_display} adapter on port {effective_port}...")
    click.echo(f"  Name:    {config.get('name', adapter_type_display)}")
    if adapter_type_display == "ollama":
        click.echo(f"  Model:   {config['model']}")
    elif adapter_type_display == "openclaw":
        click.echo(f"  Thinking: {config.get('thinking', 'low')}")
    elif adapter_type_display == "n8n":
        wh = config.get("webhook_url") or webhook_url
        click.echo(f"  Webhook: {wh}")
    elif adapter_type_display == "hermes":
        m = config.get("model")
        click.echo(f"  Model:    {m if m else '(from Hermes ~/.hermes/config.yaml)'}")
        ts = config.get("enabled_toolsets") or ["hermes-cli"]
        click.echo(f"  Toolsets: {', '.join(ts)}")
        if config.get("provider"):
            click.echo(f"  Provider: {config['provider']}")
    if config_path:
        click.echo(f"  Config:  {config_path}")
    click.echo("")

    logging.getLogger("a2a").setLevel(logging.WARNING)
    serve_agent(adapter, port=effective_port, access_log=False)
