"""Discover agents matching a query."""

import asyncio
from hybro_sdk import HybroGateway

API_KEY = "hba_your_api_key_here"


async def main():
    async with HybroGateway(api_key=API_KEY) as gw:
        agents = await gw.discover("legal contract review", limit=5)
        for agent in agents:
            print(f"[{agent.match_score:.2f}] {agent.agent_id}: {agent.agent_card.get('name')}")
            print(f"  URL: {agent.agent_card.get('url')}")
            print(f"  Description: {agent.agent_card.get('description', 'N/A')}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
