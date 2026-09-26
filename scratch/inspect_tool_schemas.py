import asyncio
import json
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def inspect_tools():
    settings = Settings.load()
    contracts = Contracts(settings.root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        resp = await gateway._session.list_tools()
        for t in resp.tools:
            print("Tool:", t.name)
            print("Description:", t.description)
            print("InputSchema:", json.dumps(t.input_schema, indent=2))
            print("-" * 50)

if __name__ == "__main__":
    asyncio.run(inspect_tools())
