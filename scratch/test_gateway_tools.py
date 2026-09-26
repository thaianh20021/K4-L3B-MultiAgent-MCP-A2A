import asyncio, json
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def test_case_10():
    settings = Settings.load()
    contracts = Contracts(settings.root / 'contracts' / 'schemas')
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        case_id = "L3B_CASE_010"
        order_id = "64a8b3b4c19751b51d211deb32fdc67b"
        
        # Test get_order_items
        try:
            it = await gateway.call("get_order_items", case_id=case_id, order_id=order_id)
            print("get_order_items success:", json.dumps(it["data"], indent=2)[:300])
        except Exception as e:
            print("get_order_items err:", e)

        # Test get_customer_history
        try:
            ch = await gateway.call("get_customer_history", case_id=case_id, customer_unique_id="customer-1deb32fdc67b")
            print("get_customer_history success:", json.dumps(ch["data"], indent=2)[:300])
        except Exception as e:
            print("get_customer_history err:", e)

        # Test get_product_context
        try:
            pc = await gateway.call("get_product_context", case_id=case_id, order_id=order_id)
            print("get_product_context success:", json.dumps(pc["data"], indent=2)[:300])
        except Exception as e:
            print("get_product_context err:", e)

if __name__ == "__main__":
    asyncio.run(test_case_10())
