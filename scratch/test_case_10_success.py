import asyncio, json
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def test_case_10_success():
    settings = Settings.load()
    contracts = Contracts(settings.root / 'contracts' / 'schemas')
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        case_id = "L3B_CASE_010"
        order_id = "64a8b3b4c19751b51d211deb32fdc67b"
        
        ord_res = await gateway.call("get_order", case_id=case_id, order_id=order_id)
        print("ORDER DATA:", json.dumps(ord_res["data"], indent=2))

        shp_res = await gateway.call("get_shipment_summary", case_id=case_id, order_id=order_id)
        print("SHIPMENT DATA:", json.dumps(shp_res["data"], indent=2))

        pmt_res = await gateway.call("get_order_payments", case_id=case_id, order_id=order_id)
        print("PAYMENT DATA:", json.dumps(pmt_res["data"], indent=2))

if __name__ == "__main__":
    asyncio.run(test_case_10_success())
