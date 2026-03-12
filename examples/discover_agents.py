"""Discover agents matching a query."""

import asyncio
from hybro_hub import HybroGateway

API_KEY = "hybro_YETntgHIYpLrmHeOzWuJjt56UB9nlC_a"


async def main():
    async with HybroGateway(api_key=API_KEY, base_url="http://localhost:8000/api/v1") as gw:
        agents = await gw.discover("Legal contract review", limit=5)
        for agent in agents:
            print(f"[{agent.match_score:.2f}] {agent.agent_id}: {agent.agent_card.get('name')}")
            print(f"  URL: {agent.agent_card.get('url')}")
            print(f"  Description: {agent.agent_card.get('description', 'N/A')}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
