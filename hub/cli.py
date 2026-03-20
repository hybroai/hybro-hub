"""Hub CLI entry points."""

from __future__ import annotations

import asyncio
import logging
import sys

import click
import httpx

from .config import load_config, save_api_key
from .main import HubDaemon
from .relay_client import RelayClient


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
@click.pass_context
def start(ctx: click.Context, api_key: str | None) -> None:
    """Start the hub daemon (foreground)."""
    if api_key:
        save_api_key(api_key)

    config = load_config(api_key=api_key)
    if not config.api_key:
        click.echo(
            "Error: No API key configured.\n"
            "Run: hybro-hub start --api-key hybro_...\n"
            "Or set HYBRO_API_KEY environment variable.",
            err=True,
        )
        sys.exit(1)

    daemon = HubDaemon(config)
    asyncio.run(daemon.run())


# ──── hybro-hub status ────

@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show hub status from the relay service."""
    config = load_config()
    if not config.api_key:
        click.echo("Error: No API key configured.", err=True)
        sys.exit(1)

    async def _status() -> None:
        relay = RelayClient(
            gateway_url=config.gateway_url,
            hub_id=config.hub_id,
            api_key=config.api_key,
        )
        try:
            data = await relay.get_status()
            hubs = data.get("hubs", [])
            if not hubs:
                click.echo("No hubs registered.")
                return
            for h in hubs:
                online = "Online" if h.get("is_online") else "Offline"
                total = h.get("agent_count", 0)
                active = h.get("active_agent_count", 0)
                inactive = h.get("inactive_agent_count", 0)
                click.echo(f"  Hub {h['hub_id'][:12]}... — {online}")
                click.echo(f"    Total agents: {total}")
                click.echo(f"    Active:       {active}")
                click.echo(f"    Inactive:     {inactive}")
        finally:
            await relay.close()

    asyncio.run(_status())


# ──── hybro-hub agents ────

@main.command()
@click.pass_context
def agents(ctx: click.Context) -> None:
    """List discovered local agents."""
    from .agent_registry import AgentRegistry

    config = load_config()

    async def _agents() -> None:
        registry = AgentRegistry(config)
        found = await registry.discover()
        await registry.close()
        if not found:
            click.echo("No local agents found.")
            return
        for a in found:
            health = "healthy" if a.healthy else "unhealthy"
            click.echo(f"  {a.name} — {a.url} — {health} (id={a.local_agent_id})")

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
@click.argument("adapter_type", type=click.Choice(sorted(_CLI_ADAPTERS), case_sensitive=False))
@click.option("--port", default=10010, type=int, help="Port for the A2A server.")
@click.option("--name", "agent_name", default=None, help="Agent display name.")
@click.option("--model", default=None, help="[ollama] Model name (default: llama3.2).")
@click.option("--system-prompt", default=None, help="[ollama] System prompt.")
@click.option("--thinking", default=None, help="[openclaw] Thinking level (off/minimal/low/medium/high/xhigh).")
@click.option("--agent-id", default=None, help="[openclaw] OpenClaw agent ID.")
@click.option("--openclaw-path", default=None, help="[openclaw] Path to openclaw binary.")
@click.option("--webhook-url", default=None, help="[n8n] Webhook URL (required for n8n).")
@click.option("--timeout", default=None, type=int, help="Request timeout in seconds.")
def agent_start(
    adapter_type: str,
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
    """
    try:
        from a2a_adapter import serve_agent
        from a2a_adapter.loader import load_adapter
    except ImportError:
        click.echo(
            "Error: a2a-adapter package not installed.\n"
            f"Install with: {_CLI_ADAPTERS[adapter_type]['install_hint']}",
            err=True,
        )
        sys.exit(1)

    config: dict = {"adapter": adapter_type}

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
        adapter = load_adapter(config)
    except ImportError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Starting {adapter_type} A2A adapter (port={port})...")
    click.echo(f"  Name: {config.get('name', adapter_type)}")
    if adapter_type == "ollama":
        click.echo(f"  Model: {config['model']}")
    elif adapter_type == "openclaw":
        click.echo(f"  Thinking: {config.get('thinking', 'low')}")
    elif adapter_type == "n8n":
        click.echo(f"  Webhook: {webhook_url}")

    serve_agent(adapter, port=port)
