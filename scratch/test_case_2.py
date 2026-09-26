import asyncio
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def test_case_2():
    settings = Settings.load()
    contracts = Contracts(settings.root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        case_id = "L3B_CASE_002"
        order_id = "5b9e9502eca103a28ea22a94c65e3950"
        
        # Test get_order
        res = await gateway._session.call_tool("get_order", arguments={"case_id": case_id, "order_id": order_id})
        print("get_order is_error:", getattr(res, "is_error", False))
        for b in res.content:
            print("Content:", getattr(b, "text", "")[:200])

        # Test get_order_items
        res_items = await gateway._session.call_tool("get_order_items", arguments={"case_id": case_id, "order_id": order_id})
        print("get_order_items is_error:", getattr(res_items, "is_error", False))
        for b in res_items.content:
            print("Items Content:", getattr(b, "text", "")[:300])

if __name__ == "__main__":
    asyncio.run(test_case_2())
