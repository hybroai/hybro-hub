"""Data models for the Hybro SDK."""

from __future__ import annotations

from pydantic import BaseModel


class AgentInfo(BaseModel):
    """An agent returned by the gateway discovery endpoint."""

    agent_id: str
    agent_card: dict
    match_score: float


class DiscoveryResponse(BaseModel):
    """Full response from gateway discovery."""

    query: str
    agents: list[AgentInfo]
    count: int


class StreamEvent(BaseModel):
    """A single SSE event from a streaming agent response.

    The raw ``data`` field preserves the full JSON payload so callers can
    inspect any A2A event type without the SDK needing to know every schema.
    """

    data: dict

    @property
    def is_error(self) -> bool:
        return "error" in self.data
