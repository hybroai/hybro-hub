# hybro-sdk

Python SDK for the **Hybro Gateway API** — discover and communicate with cloud A2A agents through the Hybro platform.

## Installation

```bash
pip install hybro-sdk
```

Or with [A2A SDK](https://pypi.org/project/a2a-sdk/) types for advanced usage:

```bash
pip install "hybro-sdk[a2a]"
```

## Quickstart

```python
import asyncio
from hybro_sdk import HybroGateway

async def main():
    async with HybroGateway(api_key="hba_...") as gw:
        # Discover agents
        agents = await gw.discover("legal contract review")
        print(f"Found {len(agents)} agents")

        # Send a synchronous message
        result = await gw.send(agents[0].agent_id, "Review this contract...")
        print(result)

        # Stream a response
        async for event in gw.stream(agents[0].agent_id, "Summarize findings"):
            print(event.data)

        # Get an agent card
        card = await gw.get_card(agents[0].agent_id)
        print(card)

asyncio.run(main())
```

## API Reference

### `HybroGateway(api_key, base_url="https://api.hybro.ai/api/v1", timeout=120.0)`

The main client. Supports `async with` for automatic cleanup.

| Parameter  | Type  | Description |
|-----------|-------|-------------|
| `api_key` | `str` | Your Hybro API key (starts with `hba_`) |
| `base_url` | `str` | Gateway base URL |
| `timeout` | `float` | Request timeout in seconds |

### Methods

#### `discover(query, *, limit=None) -> list[AgentInfo]`

Search for agents matching a natural-language query.

```python
agents = await gw.discover("data analysis", limit=10)
for a in agents:
    print(a.agent_id, a.agent_card["name"], a.match_score)
```

#### `send(agent_id, text, *, context_id=None) -> dict`

Send a synchronous message and get the full A2A response.

```python
result = await gw.send("agent-123", "What is 2+2?")
```

#### `stream(agent_id, text, *, context_id=None) -> AsyncIterator[StreamEvent]`

Stream a message and receive SSE events as they arrive.

```python
async for event in gw.stream("agent-123", "Write a story"):
    if event.is_error:
        print("Error:", event.data)
        break
    print(event.data)
```

#### `get_card(agent_id) -> dict`

Fetch an agent's card with gateway-masked URL.

```python
card = await gw.get_card("agent-123")
print(card["agent_card"]["name"])
```

## Error Handling

The SDK raises typed exceptions for common error scenarios:

```python
from hybro_sdk import (
    GatewayError,
    AuthError,
    AccessDeniedError,
    AgentNotFoundError,
    AgentCommunicationError,
    RateLimitError,
)

try:
    result = await gw.send(agent_id, "Hello")
except AuthError:
    print("Invalid API key")
except AccessDeniedError:
    print("No access to this agent")
except AgentNotFoundError:
    print("Agent not found or inactive")
except RateLimitError as e:
    print(f"Rate limited, retry after {e.retry_after}s")
except AgentCommunicationError:
    print("Upstream agent failed to respond")
except GatewayError as e:
    print(f"Gateway error ({e.status_code}): {e}")
```

These typed exceptions are raised consistently for both synchronous (`send`) and streaming (`stream`) calls. For `stream()`, authentication and access errors are raised *before* the SSE connection opens, while mid-stream upstream failures are yielded as error events (`event.is_error`).

| Exception | HTTP Status | When |
|-----------|-------------|------|
| `AuthError` | 401 | Missing or invalid API key |
| `AccessDeniedError` | 403 | No access to private agent |
| `AgentNotFoundError` | 404 | Agent doesn't exist or is inactive |
| `RateLimitError` | 429 | Rate limit exceeded |
| `AgentCommunicationError` | 502 | Upstream agent failed |
| `GatewayError` | any | Base class for all errors |

## Models

### `AgentInfo`

Returned by `discover()`.

- `agent_id: str` — unique agent identifier
- `agent_card: dict` — full A2A AgentCard (gateway-masked URL)
- `match_score: float` — similarity score (0–1)

### `StreamEvent`

Yielded by `stream()`.

- `data: dict` — raw JSON payload from the SSE event
- `is_error: bool` — `True` if the event contains an error

## Development

```bash
git clone https://github.com/hybro-ai/hybro-hub.git
cd hybro-hub
pip install -e ".[dev]"
pytest
```

## License

MIT
