"""Hub CLI entry points."""

from __future__ import annotations

import asyncio
import logging
import os
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


def _remove_lock_file() -> None:
    """Delete the lock file after a clean stop. Silent no-op if already gone."""
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


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


def _detach_windows() -> None:
    """Re-launch the current command as a detached background process (Windows only).

    Passes HYBRO_HUB_DAEMON_CHILD=1 in the environment so the child knows it
    should run as the daemon instead of spawning yet another child.
    stdout/stderr of the child are redirected to the log file.
    """
    import subprocess

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
    import signal

    import psutil

    pid = read_lock_pid()
    if pid is None:
        click.echo("Hub daemon is not running.")
        sys.exit(0)

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        click.echo(f"Hub daemon is not running (stale PID {pid} — cleaning up).")
        _remove_lock_file()
        sys.exit(0)

    # Guard against stale PID reuse: verify the process looks like hybro-hub.
    try:
        cmdline = proc.cmdline()
        is_hub = any("hybro" in part.lower() or "hub" in part.lower() for part in cmdline)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        click.echo(f"Hub daemon is not running (stale PID {pid} — cleaning up).")
        _remove_lock_file()
        sys.exit(0)

    if not is_hub:
        click.echo(
            f"PID {pid} belongs to a different process — lock file is stale.\n"
            f"Run: rm {LOCK_FILE}"
        )
        sys.exit(1)

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
        _remove_lock_file()
        click.echo("Hub daemon stopped.")
        return

    # Still alive — force kill
    click.echo(f"Daemon did not stop after {_STOP_TIMEOUT}s — force killing.")
    try:
        proc.kill()
    except psutil.NoSuchProcess:
        pass
    _remove_lock_file()
    click.echo("Hub daemon killed.")


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

    click.echo("")

    # ── Cloud relay section ───────────────────────────────────────────────────
    config = load_config()
    if config.cloud.api_key is None:
        click.echo("     Cloud relay:   No API key — run: hybro-hub start --api-key hybro_...")
        return

    async def _cloud_status() -> None:
        stop_event = asyncio.Event()

        async def _animate() -> None:
            frames = ["   ", ".  ", ".. ", "..."]
            i = 0
            while True:
                click.echo(
                    f"\r  Checking cloud relay{frames[i % len(frames)]}",
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

        relay = RelayClient(
            gateway_url=config.cloud.gateway_url,
            hub_id=config.hub_id,
            api_key=config.cloud.api_key or "",
        )
        try:
            data = await relay.get_status()
            hubs = data.get("hubs", [])
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
                click.echo(f"     Agents:        {total} total  {active} active  {inactive} inactive")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                click.echo("  ✗  Cloud relay    Authentication failed — check your API key.")
            elif exc.response.status_code == 403:
                click.echo("  ✗  Cloud relay    Access denied.")
            else:
                click.echo(f"  ✗  Cloud relay    Error {exc.response.status_code} from server.")
        except Exception as exc:
            click.echo(f"  ✗  Cloud relay    Unreachable ({exc})")
        finally:
            if anim_task is not None:
                stop_event.set()
                await anim_task
            await relay.close()

    asyncio.run(_cloud_status())


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
    timeout: int | None,
) -> None:
    """Start a local A2A agent adapter.

    Supported adapters: ollama, openclaw, n8n.

    \b
    Examples:
      hybro-hub agent start ollama
      hybro-hub agent start ollama --model mistral:7b --port 10020
      hybro-hub agent start openclaw --thinking medium
      hybro-hub agent start n8n --webhook-url http://localhost:5678/webhook/agent
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
        # CLI flags override config file values
        if port != 10010:
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

    try:
        adapter = load_adapter(config)
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
    if config_path:
        click.echo(f"  Config:  {config_path}")
    click.echo("")

    serve_agent(adapter, port=effective_port)
