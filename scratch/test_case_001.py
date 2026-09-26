import asyncio
import json
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def test_case_001():
    settings = Settings.load()
    contracts = Contracts(settings.root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        case_id = "L3B_CASE_001"
        order_id = "af0bbb47f125381ce9f3597dc70ef07b"
        
        # Tools:
        order = await gateway.call("get_order", case_id=case_id, order_id=order_id)
        print("ORDER:", json.dumps(order["data"], indent=2))

        shipment = await gateway.call("get_shipment_summary", case_id=case_id, order_id=order_id)
        print("SHIPMENT:", json.dumps(shipment["data"], indent=2))

        items = await gateway.call("get_order_items", case_id=case_id, order_id=order_id)
        print("ITEMS:", json.dumps(items["data"], indent=2))

        payments = await gateway.call("get_order_payments", case_id=case_id, order_id=order_id)
        print("PAYMENTS:", json.dumps(payments["data"], indent=2))

        policy = await gateway.call("get_policy", case_id=case_id, policy_version="EC_POLICY_V2")
        print("POLICY:", json.dumps(policy["data"], indent=2))

if __name__ == "__main__":
    asyncio.run(test_case_001())
