from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from student_agent import VARIANT_ID
from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case
from test_workflow import ROOT, FakeGateway, sample


def artifacts(root: Path) -> tuple[CaseSet, Contracts, list[dict]]:
    case, data = sample()
    contracts = Contracts(ROOT / "contracts/schemas")
    trace = TraceWriter(root / "traces/trace.jsonl", contracts)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, FakeGateway(data), trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    (root / "outputs").mkdir()
    (root / "outputs" / f"{case['case_id']}.json").write_text(json.dumps(output), encoding="utf-8")
    events = [json.loads(line) for line in trace.path.read_text("utf-8").splitlines()]
    case_set = CaseSet("test", VARIANT_ID, (case["case_id"],), {case["case_id"]: case})
    return case_set, contracts, events


def test_full_lifecycle_and_evidence_linkage_pass(tmp_path):
    case_set, contracts, events = artifacts(tmp_path)
    outputs, lines = validate_artifacts(tmp_path, case_set, contracts)
    assert len(outputs) == 1
    assert len(lines) == len(events)


@pytest.mark.parametrize("failure", ["missing_verifier", "wrong_order", "unlinked_evidence"])
def test_schema_valid_but_inconsistent_trace_is_rejected(tmp_path, failure):
    case_set, contracts, events = artifacts(tmp_path)
    if failure == "missing_verifier":
        events = [event for event in events if event["event_type"] != "verification_completed"]
    elif failure == "wrong_order":
        events[0], events[-1] = events[-1], events[0]
    else:
        events = [event for event in events if event["event_type"] != "tool_result_consumed"]
    (tmp_path / "traces/trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        validate_artifacts(tmp_path, case_set, contracts)
