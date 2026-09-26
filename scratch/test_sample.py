import asyncio, json
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case
from pathlib import Path

async def test_sample_cases():
    settings = Settings.load()
    contracts = Contracts(settings.root / 'contracts' / 'schemas')
    trace_path = Path("traces/test_trace.jsonl")
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for cid in ["L3B_CASE_001", "L3B_CASE_002", "L3B_CASE_010"]:
            print(f"=== Testing {cid} ===")
            with open(f"inputs/{cid}.json", encoding="utf-8") as f:
                case_data = json.load(f)
            output = await solve_case(case_data, gateway, trace)
            contracts.validate_output(output, f"test_{cid}.json")
            print(f"{cid} validation SUCCESS!")
            print(json.dumps(output, indent=2)[:500])

if __name__ == "__main__":
    asyncio.run(test_sample_cases())
