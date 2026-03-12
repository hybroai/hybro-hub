"""Send a synchronous message to an agent."""

import asyncio
import json
from hybro_hub import HybroGateway

API_KEY = "hybro_your_api_key_here"
AGENT_ID = "your-agent-id"


async def main():
    async with HybroGateway(api_key=API_KEY) as gw:
        result = await gw.send(AGENT_ID, "Review the following contract clause: ...")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
