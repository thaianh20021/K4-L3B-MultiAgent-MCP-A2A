import asyncio, json
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def inspect_cases():
    settings = Settings.load()
    contracts = Contracts(settings.root / 'contracts' / 'schemas')
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for cid, oid in [("L3B_CASE_002", "5b9e9502eca103a28ea22a94c65e3950"), ("L3B_CASE_003", "adc253f1977c81c2c29dc7b9dd23d538")]:
            print(f"=== {cid} ===")
            shp = await gateway.call("get_shipment_summary", case_id=cid, order_id=oid)
            print("SHIPMENT:", json.dumps(shp["data"], indent=2))
            pmt = await gateway.call("get_order_payments", case_id=cid, order_id=oid)
            print("PAYMENTS:", json.dumps(pmt["data"], indent=2))

if __name__ == "__main__":
    asyncio.run(inspect_cases())
