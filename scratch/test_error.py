import asyncio
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def test_error():
    settings = Settings.load()
    contracts = Contracts(settings.root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        case_id = "L3B_CASE_001"
        order_id = "af0bbb47f125381ce9f3597dc70ef07b"
        
        raw_res = await gateway._session.call_tool("get_order", arguments={"case_id": case_id, "order_id": order_id})
        print("is_error:", getattr(raw_res, "is_error", None))
        for b in raw_res.content:
            print("Content block:", getattr(b, "text", b))

if __name__ == "__main__":
    asyncio.run(test_error())
