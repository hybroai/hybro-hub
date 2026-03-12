# Hybro Hub

**Your local AI agents, accessible from anywhere. Your data stays on your machine.**

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

### 4. Launch a local agent

Start a local LLM as an A2A agent (requires [Ollama](https://ollama.com) installed):

```bash
hybro-hub agent start ollama --model llama3.2:8b
```

You'll see:

```
🔗 Connected to hybro.ai
📡 Found 1 local agent:
   • My Ollama Chat (llama3.2:8b) — localhost:10010
Agents synced to hybro.ai. Open hybro.ai to start chatting.
```

### 5. Open hybro.ai

Refresh [hybro.ai](https://hybro.ai). Your local agent appears alongside cloud agents:

```
  ☁️  Legal Contract Reviewer          (cloud)
  ☁️  Code Review Pro                  (cloud)
  🏠  My Ollama Chat (llama3.2:8b)     (local · online)
```

Add it to a room, send a message. The response streams back with a **🏠 Local** badge — your data never left your machine.

---

## How It Works

```
┌─────────────────────────────────────────────────┐
│  Your Machine                                   │
│                                                 │
│   Hybro Hub (background daemon)                 │
│   ├── Your local agents (Ollama, custom, etc.)  │
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

> Phase 2 logs detections. Active blocking and anonymization are on the roadmap.

---

## CLI Reference

### `hybro-hub start`

Start the hub daemon. Connects to hybro.ai, discovers local agents, and syncs them to the cloud.

```bash
hybro-hub start --api-key hybro_...
```

The API key is saved to `~/.hybro/config.yaml` after first use — subsequent starts don't need it.

### `hybro-hub status`

Check if the hub is connected and how many agents are synced.

```bash
hybro-hub status
```

### `hybro-hub agents`

List all discovered local agents and their health status.

```bash
hybro-hub agents
```

### `hybro-hub agent start`

Launch a local A2A agent from a bundled adapter. Supported adapters: **ollama**, **openclaw**, **n8n**.

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

**Common options:**

| Option      | Default | Description                   |
| ----------- | ------- | ----------------------------- |
| `--port`    | `10010` | Port for the A2A agent server |
| `--name`    | auto    | Agent display name            |
| `--timeout` | varies  | Request timeout in seconds    |

**Adapter-specific options:**

| Option            | Adapter  | Description                                       |
| ----------------- | -------- | ------------------------------------------------- |
| `--model`         | ollama   | Ollama model (default: `llama3.2:8b`)             |
| `--system-prompt` | ollama   | Custom system prompt                              |
| `--thinking`      | openclaw | Thinking level: off/minimal/low/medium/high/xhigh |
| `--agent-id`      | openclaw | OpenClaw agent ID                                 |
| `--openclaw-path` | openclaw | Path to the openclaw binary                       |
| `--webhook-url`   | n8n      | Webhook URL (required)                            |

> Requires the `a2a-adapter` package: `pip install a2a-adapter`

---

## Configuration

The hub reads from `~/.hybro/config.yaml`. A full example:

```yaml
# Cloud connection
cloud:
  api_key: "hybro_..."
  gateway_url: "https://api.hybro.ai"

# Agent discovery
agents:
  auto_discover: true # Probe localhost ports for A2A agents
  auto_discover_exclude_ports: # Skip non-agent ports
    - 22 # SSH
    - 3306 # MySQL
    - 5432 # PostgreSQL
  local: # Always-registered agents
    - name: "My Custom Agent"
      url: "http://localhost:9001"

# Privacy
privacy:
  default_routing: "local_first"
  sensitive_keywords: ["medical", "financial", "confidential"]
  sensitive_patterns: ["PROJ-\\d{4}"]

# Heartbeat (seconds)
heartbeat_interval: 30
```

You can also set the API key and gateway URL via environment variables:

```bash
export HYBRO_API_KEY="hybro_..."
export HYBRO_GATEWAY_URL="https://api.hybro.ai"
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

Use the [a2a-python SDK](https://github.com/a2aproject/A2A/tree/main/sdks/python) to build a compatible agent:

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
