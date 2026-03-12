"""Stream a response from an agent via SSE."""

import asyncio
from hybro_hub import HybroGateway

API_KEY = "hybro_your_api_key_here"
AGENT_ID = "your-agent-id"


async def main():
    async with HybroGateway(api_key=API_KEY) as gw:
        async for event in gw.stream(AGENT_ID, "Write a summary of this document..."):
            if event.is_error:
                print(f"ERROR: {event.data}")
                break
            print(event.data)


if __name__ == "__main__":
    asyncio.run(main())
