import asyncio
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def test_all():
    settings = Settings.load()
    contracts = Contracts(settings.root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        case_id = "L3B_CASE_001"
        order_id = "af0bbb47f125381ce9f3597dc70ef07b"
        customer_unique_id = "customer-597dc70ef07b"
        
        test_calls = [
            ("get_customer_history", {"customer_unique_id": customer_unique_id}),
            ("get_order", {"order_id": order_id}),
            ("get_order_items", {"order_id": order_id}),
            ("get_order_payments", {"order_id": order_id}),
            ("get_payment_timeline", {"order_id": order_id}),
            ("get_shipment_summary", {"order_id": order_id}),
            ("get_sellers", {"order_id": order_id}),
            ("get_product_context", {"order_id": order_id}),
            ("get_refund_timeline", {"order_id": order_id}),
            ("get_policy", {"policy_version": "EC_POLICY_V2"}),
        ]
        
        for name, args in test_calls:
            raw = await gateway._session.call_tool(name, arguments={"case_id": case_id, **args})
            is_err = getattr(raw, "is_error", False)
            content = " ".join(b.text for b in raw.content if getattr(b, "text", None))
            print(f"Tool {name}: is_error={is_err}, content_len={len(content)}")
            if is_err:
                print(f"   Error detail: {content}")
            else:
                print(f"   Success! sample: {content[:100]}...")

if __name__ == "__main__":
    asyncio.run(test_all())
