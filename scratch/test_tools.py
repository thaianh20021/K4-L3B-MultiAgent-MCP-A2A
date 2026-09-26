import asyncio
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def test_tools():
    settings = Settings.load()
    contracts = Contracts(settings.root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        case_id = "L3B_CASE_001"
        order_id = "af0bbb47f125381ce9f3597dc70ef07b"
        
        # 1. get_policy
        p = await gateway.call("get_policy", case_id=case_id, policy_version="EC_POLICY_V2")
        print("POLICY ref:", p.get("evidence_ref"))
        
        # 2. get_customer_history
        c = await gateway.call("get_customer_history", case_id=case_id, customer_unique_id="customer-597dc70ef07b")
        print("CUSTOMER ref:", c.get("evidence_ref"))
        print("CUSTOMER data:", c.get("data"))

if __name__ == "__main__":
    asyncio.run(test_tools())
