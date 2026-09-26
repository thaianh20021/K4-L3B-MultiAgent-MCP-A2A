import asyncio
import json
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def test():
    settings = Settings.load()
    contracts = Contracts(settings.root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        tools = await gateway.list_tools()
        print("Tools:", tools)
        
        # Test case 1
        res1 = await gateway.call("get_order", case_id="L3B_CASE_001", order_id="af0bbb47f125381ce9f3597dc70ef07b")
        print("get_order res1:", json.dumps(res1, indent=2))

        res2 = await gateway.call("get_customer_history", case_id="L3B_CASE_001", customer_unique_id="customer-597dc70ef07b")
        print("get_customer_history res2:", json.dumps(res2, indent=2))

if __name__ == "__main__":
    asyncio.run(test())
