import asyncio, json
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def inspect_1_to_10():
    settings = Settings.load()
    contracts = Contracts(settings.root / 'contracts' / 'schemas')
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for i in range(1, 11):
            cid = f"L3B_CASE_{i:03d}"
            with open(f"inputs/{cid}.json", encoding="utf-8") as f:
                d = json.load(f)
            oid = [c for c in d["candidate_order_ids"] if len(c) == 32][0]
            claim = [c["topic"] for c in d["customer_request"]["claims"] if c["topic"] != "requested_full_refund"][0]
            
            # Call tools
            ord_res = await gateway.call("get_order", case_id=cid, order_id=oid)
            shp_res = await gateway.call("get_shipment_summary", case_id=cid, order_id=oid)
            pmt_res = await gateway.call("get_order_payments", case_id=cid, order_id=oid)
            
            print(f"=== {cid} (Claim: {claim}) ===")
            print("Order status:", ord_res["data"].get("order_status"))
            print("Shipment status:", shp_res["data"].get("order_status"), "Events:", shp_res["data"].get("events"))
            print("Delivery times: delivered_customer=", shp_res["data"].get("delivered_customer_at"), "estimated=", shp_res["data"].get("estimated_delivery_at"), "carrier=", shp_res["data"].get("delivered_carrier_at"))
            print("Payments count:", len(pmt_res["data"]) if isinstance(pmt_res["data"], list) else pmt_res["data"])

if __name__ == "__main__":
    asyncio.run(inspect_1_to_10())
