# Hybro Hub

**Your local & remote AI agents — private, powerful, unified.**

[![PyPI](https://img.shields.io/pypi/v/hybro-hub)](https://pypi.org/project/hybro-hub/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)

Hybro Hub is a lightweight daemon that connects your local AI agents to [hybro.ai](https://hybro.ai) — so you can use local and cloud agents side by side in one portal, with full control over where your data goes.

```
pip install hybro-hub
```

---

## The Problem

AI agents today force a choice:

- **Cloud platforms** (ChatGPT, Devin, Cursor Cloud) are powerful but require sending your data to third-party servers.
- **Local runtimes** (Ollama, LM Studio) keep data private but are isolated — no access to specialized cloud agents, no shared UI.

You shouldn't have to choose between **privacy** and **power**.

## The Solution

Hybro Hub bridges local and cloud. Open [hybro.ai](https://hybro.ai), see your local Ollama model next to cloud agents like a legal reviewer or code analyst. Chat with any of them. Your local agents process on your machine — your data never leaves. Cloud agents are there when you need more capability.

One portal. Your choice, per conversation.

---

## Get Started in 5 Minutes

### 1. Install

```bash
pip install hybro-hub
```

### 2. Get your API key

Go to [hybro.ai/d/discovery-api-keys](https://hybro.ai/d/discovery-api-keys) → API Keys → **Generate New Key**. Copy the key (starts with `hybro_`).

### 3. Start the hub

```bash
hybro-hub start --api-key hybro_your_key_here
```

The hub starts as a **background daemon** and returns you to the prompt immediately. Logs are written to `~/.hybro/hub.log`. The API key is saved to `~/.hybro/config.yaml` — subsequent starts don't need it.

```bash
hybro-hub status   # check local daemon state and cloud connection
hybro-hub stop     # stop the daemon gracefully
```

### 4. Launch a local agent

Start a local LLM as an A2A agent (requires [Ollama](https://ollama.com) installed):

```bash
hybro-hub agent start ollama --model llama3.2
```

You'll see:

```
🔗 Connected to hybro.ai
📡 Found 1 local agent:
   • My Ollama Chat (llama3.2) — localhost:10010
Agents synced to hybro.ai. Open hybro.ai to start chatting.
```

### 5. Open hybro.ai

Refresh [hybro.ai](https://hybro.ai). Your local agent appears alongside cloud agents:

```
  ☁️  Legal Contract Reviewer          (cloud)
  ☁️  Code Review Pro                  (cloud)
  🏠  My Ollama Chat (llama3.2)     (local · online)
```

Add it to a room, send a message. The response streams back with a **🏠 Local** badge — your data never left your machine.

---

## How It Works

```
┌─────────────────────────────────────────────────┐
│  Your Machine                                   │
│                                                 │
│   Hybro Hub (background daemon)                 │
│   ├── Your local agents (Ollama, Hermes, etc.)  │
│   ├── Privacy router                            │
│   └── Relay client ──outbound HTTPS only──┐     │
│                                           │     │
└───────────────────────────────────────────┼─────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────┐
│  hybro.ai Cloud                                 │
│                                                 │
│   ├── Web portal (your browser)                 │
│   ├── Cloud agents (marketplace)                │
│   ├── Relay service (routes to your hub)        │
│   └── Message history & rooms                   │
└─────────────────────────────────────────────────┘
```

**Key properties:**

- **Outbound-only** — the hub initiates all connections. No inbound ports, no firewall changes, works behind NAT.
- **Portal-first** — you always use hybro.ai. No localhost URLs, no mode switching. Local agents just appear as more agents in the same portal.
- **A2A protocol** — local and cloud agents speak the same [Agent-to-Agent protocol](https://github.com/a2aproject/A2A). Any A2A-compatible agent works.
- **Graceful degradation** — if the hub is offline, cloud agents still work. Local agents show as "offline" and messages queue until the hub reconnects.

---

## Privacy by Architecture

Hybro Hub doesn't just promise privacy — the architecture enforces it.

**Your data stays local when you use local agents.** Messages to local agents route through the relay to your hub, get processed entirely on your machine, and only the response travels back. The cloud relay sees message metadata (routing info), not your content.

### Privacy indicators in the UI

Every message in hybro.ai shows where it was processed:

- 🏠 **Local** (green) — processed on your machine, data did not leave
- ☁️ **Cloud** (blue) — processed by a cloud agent via hybro.ai

### Sensitivity detection

The hub scans outbound messages for sensitive content before they reach cloud agents:

- **PII detection** — emails, phone numbers, SSNs, credit cards, API keys
- **Custom keywords** — configure terms like "medical", "financial", "confidential"
- **Custom patterns** — add regex rules for project-specific data (e.g., `PROJ-\d{4}`)

> Currently logs detections only. Active blocking and anonymization are planned for a future release.

---

## CLI Reference

### `hybro-hub start`

Start the hub daemon. Connects to hybro.ai, discovers local agents, and syncs them to the cloud. The process detaches immediately and runs in the background.

```bash
hybro-hub start --api-key hybro_...
```

The API key is saved to `~/.hybro/config.yaml` after first use — subsequent starts don't need it. Only one instance can run per machine; a second `start` will exit with an error if the daemon is already running.

**Options:**

| Option | Description |
| --- | --- |
| `--api-key` | Hybro API key (also saves to `~/.hybro/config.yaml`) |
| `--foreground`, `-f` | Run in the foreground instead of daemonizing (useful for debugging) |

Daemon logs are written to `~/.hybro/hub.log` (rotating, max 10 MB × 3 files).

### `hybro-hub stop`

Gracefully stop the background daemon. Sends `SIGTERM` and waits up to 10 seconds before sending `SIGKILL`. Removes the PID lock file on success so that `hybro-hub status` correctly shows "Stopped".

```bash
hybro-hub stop
```

### `hybro-hub restart`

Stop the running daemon (if any) and start a fresh one. If no daemon is running, starts one directly.

```bash
hybro-hub restart
hybro-hub restart --foreground   # restart attached to terminal (useful for debugging)
```

| Option | Description |
| --- | --- |
| `--foreground`, `-f` | After stopping, restart in the foreground (attached to terminal) |

### `hybro-hub update`

Check for and install upgrades to hybro-hub and its dependencies (`a2a-adapter`, `a2a-sdk`). Automatically detects whether you installed with pip, pipx, or uv and runs the correct upgrade command.

```bash
hybro-hub update              # upgrade all packages
hybro-hub update --dry-run    # check for updates without installing
hybro-hub update --restart    # upgrade and restart the daemon
```

| Option | Description |
| --- | --- |
| `--dry-run` | Check for available upgrades without installing |
| `--restart` | After upgrade, stop the running daemon (if any) and start a fresh one with the new code |

### `hybro-hub --version`

Print the installed version of hybro-hub.

```bash
hybro-hub --version
```

### `hybro-hub status`

Show the state of the local daemon and its connection to the cloud relay.

```bash
hybro-hub status
```

Example output when running:

```
  Local daemon:  Running (PID 12345)
  Log file:      /Users/you/.hybro/hub.log
  Cloud relay:   Online (hub abc123...)
  Agents:        3 total (3 active, 0 inactive)
```

Example output when stopped:

```
  Local daemon:  Stopped
  Cloud relay:   Online (hub abc123...)
  Agents:        4 total (3 active, 1 inactive)
```

> The cloud relay section queries hybro.ai directly, so it reflects the last known state even when the local daemon is not running.

### `hybro-hub agents`

List all discovered local agents and their health status.

```bash
hybro-hub agents
```

### `hybro-hub agent start`

Launch a local A2A agent from a bundled adapter. Supported adapters: **ollama**, **openclaw**, **n8n**, **hermes** ([Hermes Agent](https://github.com/NousResearch/hermes-agent) via [a2a-adapter](https://pypi.org/project/a2a-adapter/) `HermesAdapter`).

**Ollama** — local LLM (requires [Ollama](https://ollama.com)):

```bash
hybro-hub agent start ollama
hybro-hub agent start ollama --model mistral:7b --port 10020 --system-prompt "You are a helpful assistant"
```

**OpenClaw** — AI coding agent (requires [OpenClaw](https://openclaw.ai)):

```bash
hybro-hub agent start openclaw
hybro-hub agent start openclaw --thinking medium --agent-id main
```

**n8n** — workflow automation (requires a running [n8n](https://n8n.io) instance):

```bash
hybro-hub agent start n8n --webhook-url http://localhost:5678/webhook/my-agent
```

**Hermes** — multi-purpose assistant with tool use and persistent memory (requires a local [Hermes Agent](https://github.com/NousResearch/hermes-agent) checkout on `PYTHONPATH`, plus `hermes setup` for `~/.hermes/config.yaml`):

```bash
export PYTHONPATH=/path/to/hermes-agent:$PYTHONPATH
hybro-hub agent start hermes
hybro-hub agent start hermes --model anthropic/claude-sonnet-4 --enabled-toolsets hermes-cli
```

For more options (`provider`, extra toolsets, custom card text), use `--config` with a YAML file whose top-level `adapter:` is `hermes` (see [a2a-adapter examples](https://github.com/hybroai/a2a-adapter/blob/main/examples/hermes_agent.py)).

**Common options:**

| Option      | Default | Description                   |
| ----------- | ------- | ----------------------------- |
| `--port`    | `10010` | Port for the A2A agent server |
| `--name`    | auto    | Agent display name            |
| `--timeout` | varies  | Request timeout in seconds    |

**Adapter-specific options:**

| Option            | Adapter  | Description                                       |
| ----------------- | -------- | ------------------------------------------------- |
| `--model`         | ollama, hermes | Ollama: model name (default `llama3.2`). Hermes: optional model override |
| `--system-prompt` | ollama   | Custom system prompt                              |
| `--thinking`      | openclaw | Thinking level: off/minimal/low/medium/high/xhigh |
| `--agent-id`      | openclaw | OpenClaw agent ID                                 |
| `--openclaw-path` | openclaw | Path to the openclaw binary                       |
| `--webhook-url`   | n8n      | Webhook URL (required)                            |
| `--provider`      | hermes   | Provider override (optional)                      |
| `--enabled-toolsets` | hermes | Comma-separated toolsets (default: `hermes-cli`) |

`hybro-hub` depends on **a2a-adapter**; `pip install hybro-hub` installs it. To upgrade adapters and the SDK alongside the hub, run `hybro-hub update`.

---

## Configuration

The hub reads from `~/.hybro/config.yaml`. A minimal working example:

```yaml
cloud:
  api_key: ${HYBRO_API_KEY}   # or paste directly: "hybro_your_key_here"
```

A fully-annotated template is available in [`config.yaml.example`](config.yaml.example). Below is a representative example covering all sections:

```yaml
# Cloud connection
cloud:
  api_key: ${HYBRO_API_KEY}          # or ${HYBRO_API_KEY:-} to allow unset
  gateway_url: "https://api.hybro.ai"

# Agent discovery
agents:
  auto_discover: true                  # probe localhost ports for A2A agents
  auto_discover_exclude_ports:         # skip non-agent ports (built-in defaults shown)
    - 22    # SSH
    - 53    # DNS
    - 80    # HTTP
    - 443   # HTTPS
    - 3306  # MySQL
    - 5432  # PostgreSQL
    - 6379  # Redis
    - 27017 # MongoDB
  # auto_discover_scan_range: [10000, 11000]  # restrict scan to a port range
  local:                               # always-registered agents
    - name: "My Custom Agent"
      url: "http://localhost:9001"

# Privacy (classification is logging-only; messages are not blocked or rerouted)
privacy:
  sensitive_keywords: ["medical", "financial", "confidential"]
  sensitive_patterns: ["PROJ-\\d{4}"]

# Offline resilience — events that fail delivery are queued to disk and retried
publish_queue:
  enabled: true
  max_size_mb: 50
  ttl_hours: 24
  drain_interval: 30        # seconds between retry cycles
  drain_batch_size: 20
  max_retries_critical: 20  # agent_response, agent_error, processing_status
  max_retries_normal: 5     # task_submitted, artifact_update, task_status

# Heartbeat interval (seconds)
heartbeat_interval: 30
```

The config file supports `${VAR}`, `${VAR:-default}`, and `$${VAR}` (literal) environment variable references (expanded before parsing, matching the OTel Collector convention). To set the API key via a shell environment variable without editing the file, add to your shell profile:

```bash
export HYBRO_API_KEY="hybro_..."
```

Or set it once via the CLI (saves the literal key to the config file):

```bash
hybro-hub start --api-key hybro_...
```

---

## Bring Your Own Agent

Any agent that speaks the [A2A protocol](https://github.com/a2aproject/A2A) works with Hybro Hub.

### Auto-discovery

With `auto_discover: true` (the default), the hub automatically finds A2A agents running on localhost by probing listening TCP ports for agent cards at `/.well-known/agent.json` or `/.well-known/agent-card.json`. Just start your agent — the hub will find it.

### Manual registration

Add agents to `~/.hybro/config.yaml`:

```yaml
agents:
  local:
    - name: "My Research Agent"
      url: "http://localhost:8001"
    - name: "Team Agent"
      url: "http://192.168.1.50:8080" # LAN agents work too
```

### Building an A2A agent

Use the [a2a-python SDK](https://github.com/a2aproject/a2a-python) to build a compatible agent:

```python
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler

app = A2AStarletteApplication(
    agent_card=my_card,
    http_handler=DefaultRequestHandler(agent_executor=my_executor),
)
```

The hub discovers it automatically and syncs it to hybro.ai.

---

## Hybro SDK (Python Client)

The repo also ships `hybro_hub` — a Python client for calling cloud agents programmatically via the Hybro Gateway API. Use this when you want to integrate cloud agents into your own code, outside of the hub.

### Quickstart

```python
import asyncio
from hybro_hub import HybroGateway

async def main():
    async with HybroGateway(api_key="hybro_...") as gw:
        agents = await gw.discover("legal contract review")
        async for event in gw.stream(agents[0].agent_id, "Review this NDA"):
            print(event.data)

asyncio.run(main())
```

### Methods

| Method                                       | Description                                                       |
| -------------------------------------------- | ----------------------------------------------------------------- |
| `discover(query, *, limit=None)`             | Search for agents by natural language. Returns `list[AgentInfo]`. |
| `send(agent_id, text, *, context_id=None)`   | Send a message, get the full response. Returns `dict`.            |
| `stream(agent_id, text, *, context_id=None)` | Stream a response via SSE. Yields `StreamEvent`.                  |
| `get_card(agent_id)`                         | Fetch an agent's A2A card. Returns `dict`.                        |

### Error handling

```python
from hybro_hub import AuthError, RateLimitError, AgentNotFoundError

try:
    result = await gw.send(agent_id, "Hello")
except AuthError:
    print("Invalid API key")
except AgentNotFoundError:
    print("Agent not found")
except RateLimitError as e:
    print(f"Rate limited — retry after {e.retry_after}s")
```

| Exception                 | Status | Cause                      |
| ------------------------- | ------ | -------------------------- |
| `AuthError`               | 401    | Invalid API key            |
| `AccessDeniedError`       | 403    | No access to agent         |
| `AgentNotFoundError`      | 404    | Agent not found / inactive |
| `RateLimitError`          | 429    | Rate limit exceeded        |
| `AgentCommunicationError` | 502    | Upstream agent error       |
| `GatewayError`            | any    | Base class                 |

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) (optional, for the built-in Ollama adapter)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) on `PYTHONPATH` (optional, for the Hermes adapter)
- A [hybro.ai](https://hybro.ai) account with an API key

## Development

```bash
git clone https://github.com/hybro-ai/hybro-hub.git
cd hybro-hub
pip install -e ".[dev]"
pytest
```

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
