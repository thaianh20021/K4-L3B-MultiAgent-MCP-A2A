from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.trace import TraceWriter
from student_agent.workers_ai import WorkersAISettings, parse_model_object
from student_agent.workflow import collect_evidence, solve_case

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ErrorSession:
    async def call_tool(self, tool_name: str, arguments: dict[str, str]) -> CallToolResult:
        del tool_name, arguments
        return CallToolResult(
            content=[TextContent(type="text", text="not found")],
            is_error=True,
        )


class NoopContracts:
    def validate_evidence(self, value: Any, label: str) -> None:
        del value, label


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append({"tool": tool_name, "case_id": case_id, "args": arguments})
        suffix = f"{len(self.calls):024d}"
        data = {"found": True, **arguments}
        if tool_name == "get_order":
            data["order_id"] = arguments["order_id"]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix}",
            "result_hash": f"sha256:{'0' * 64}",
            "domain": "order",
            "data": data,
        }


class FlakyGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if tool_name == "get_policy" and not self.failed:
            self.failed = True
            self.calls.append({"tool": tool_name, "case_id": case_id, "args": arguments})
            raise RuntimeError("temporary MCP failure")
        return await super().call(tool_name, case_id=case_id, **arguments)


class MissingRefundGateway(FakeGateway):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if tool_name == "get_refund_timeline":
            self.calls.append({"tool": tool_name, "case_id": case_id, "args": arguments})
            raise RuntimeError("refund timeline unavailable")
        return await super().call(tool_name, case_id=case_id, **arguments)


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def sample_case() -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "customer_request": {
            "language": "vi",
            "message": "Investigate the complaint.",
            "claimed_order_id": "order-001",
            "claims": [
                {"claim_id": "claim-001-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": ["order-001", "candidate-001"],
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
        "customer_unique_id_hint": "customer-001",
    }


def sample_valid_output() -> dict[str, Any]:
    refs = [f"ev_{number:024d}" for number in range(1, 12)]
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "L3B_CASE_001",
        "assessment": {
            "primary_issue": "late_delivery_logistics",
            "secondary_issues": ["requested_full_refund"],
            "case_status": "action_required",
            "confidence": 0.9,
        },
        "affected_entities": {
            "order_ids": ["order-001"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": "claim-001-a",
                "verdict": "supported",
                "confidence": 0.9,
                "evidence_refs": refs,
            },
            {
                "claim_id": "claim-001-b",
                "verdict": "supported",
                "confidence": 0.8,
                "evidence_refs": refs,
            },
        ],
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": ["order-001"],
            "rejected_candidates": ["candidate-001"],
            "confidence": 0.95,
        },
        "customer_context": {
            "customer_unique_id": "customer-001",
            "related_order_ids": ["order-001"],
        },
        "shipment_analysis": {
            "verdict": "logistics_delay",
            "late_seller_ids": [],
            "timeline_complete": True,
        },
        "payment_analysis": {
            "verdict": "reconciled",
            "captured_total_brl": 100,
            "refunded_total_brl": 0,
            "refundable_total_brl": 100,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "LOGISTICS_DELAY", "rank": 1}],
            "responsible_parties": [
                {"party_type": "logistics_provider", "party_id": None}
            ],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 100,
            "refund_lines": [
                {
                    "reason_code": "FULL_REFUND",
                    "amount_brl": 100,
                    "entity_id": "order-001",
                }
            ],
        },
        "resolution_actions": ["Issue the approved refund"],
    }


def test_workers_ai_settings_require_all_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_AI_MODEL", "@cf/meta/test")

    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID"):
        WorkersAISettings.load()


def test_gateway_uses_current_mcp_error_attribute() -> None:
    gateway = EvidenceGateway(ErrorSession(), NoopContracts())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="missing"))


def test_parse_model_object_accepts_plain_json() -> None:
    assert parse_model_object('{"case_id":"CASE_001"}') == {"case_id": "CASE_001"}


def test_parse_model_object_accepts_mapping() -> None:
    value = {"case_id": "CASE_001"}
    assert parse_model_object(value) is value


def test_parse_model_object_accepts_fenced_json() -> None:
    text = '```json\n{"case_id":"CASE_001"}\n```'
    assert parse_model_object(text) == {"case_id": "CASE_001"}


def test_parse_model_object_ignores_trailing_commentary() -> None:
    text = '{"case_id":"CASE_001"}\nThis is the final answer.'
    assert parse_model_object(text) == {"case_id": "CASE_001"}


def test_collect_evidence_is_case_scoped_and_cached() -> None:
    gateway = FakeGateway()
    trace = FakeTrace()
    case = sample_case()

    bundle = asyncio.run(collect_evidence(case, gateway, trace))

    assert bundle["case_id"] == case["case_id"]
    assert all(call["case_id"] == case["case_id"] for call in gateway.calls)
    signatures = {
        (call["tool"], tuple(sorted(call["args"].items()))) for call in gateway.calls
    }
    assert len(gateway.calls) == len(signatures)
    assert set(bundle["evidence_refs"]) == {
        event["evidence_refs"][0]
        for event in trace.events
        if event["event_type"] == "tool_result_consumed"
    }


def test_collect_evidence_retries_one_mcp_failure() -> None:
    gateway = FlakyGateway()

    bundle = asyncio.run(collect_evidence(sample_case(), gateway, FakeTrace()))

    assert bundle["evidence_refs"]
    assert [call["tool"] for call in gateway.calls].count("get_policy") == 2


def test_collect_evidence_records_optional_tool_failure() -> None:
    gateway = MissingRefundGateway()

    bundle = asyncio.run(collect_evidence(sample_case(), gateway, FakeTrace()))

    assert bundle["failures"] == [
        {
            "tool_name": "get_refund_timeline",
            "arguments": {"order_id": "order-001"},
            "error": "refund timeline unavailable",
        }
    ]
    assert [call["tool"] for call in gateway.calls].count("get_refund_timeline") == 2


def test_solve_case_returns_valid_l3b_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = sample_valid_output()

    async def fake_request_object(prompt: str) -> dict[str, Any]:
        assert "entity-agent" in prompt
        assert "shipment-agent" in prompt
        assert "payment-refund-agent" in prompt
        assert "policy-conflict-agent" in prompt
        assert "verifier" in prompt
        return expected

    monkeypatch.setattr("student_agent.workflow.request_object", fake_request_object)
    contracts = Contracts(PROJECT_ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(solve_case(sample_case(), FakeGateway(), trace))

    contracts.validate_output(result, "test output")
    assert result["case_id"] == "L3B_CASE_001"


def test_solve_case_repairs_one_invalid_model_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    valid = sample_valid_output()
    invalid = {**valid, "case_id": "WRONG_CASE"}
    responses = iter([invalid, valid])
    prompts: list[str] = []

    async def fake_request_object(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("student_agent.workflow.request_object", fake_request_object)
    contracts = Contracts(PROJECT_ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(solve_case(sample_case(), FakeGateway(), trace))

    assert result == valid
    assert len(prompts) == 2
    assert "wrong case_id" in prompts[1]


def test_solve_case_deduplicates_scalar_arrays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = sample_valid_output()
    output["affected_entities"]["payment_references"] = ["1", "1"]
    calls = 0

    async def fake_request_object(prompt: str) -> dict[str, Any]:
        nonlocal calls
        del prompt
        calls += 1
        return output

    monkeypatch.setattr("student_agent.workflow.request_object", fake_request_object)
    contracts = Contracts(PROJECT_ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(solve_case(sample_case(), FakeGateway(), trace))

    contracts.validate_output(result, "test output")
    assert result["affected_entities"]["payment_references"] == ["1"]
    assert calls == 1


def test_solve_case_adds_empty_conflicts_when_model_omits_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = sample_valid_output()
    del output["data_conflicts"]
    calls = 0

    async def fake_request_object(prompt: str) -> dict[str, Any]:
        nonlocal calls
        del prompt
        calls += 1
        return output

    monkeypatch.setattr("student_agent.workflow.request_object", fake_request_object)
    contracts = Contracts(PROJECT_ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(solve_case(sample_case(), FakeGateway(), trace))

    contracts.validate_output(result, "test output")
    assert result["data_conflicts"] == []
    assert calls == 1


def test_solve_case_adds_zero_financial_resolution_when_model_omits_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = sample_valid_output()
    del output["financial_resolution"]

    async def fake_request_object(prompt: str) -> dict[str, Any]:
        del prompt
        return output

    monkeypatch.setattr("student_agent.workflow.request_object", fake_request_object)
    contracts = Contracts(PROJECT_ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(solve_case(sample_case(), FakeGateway(), trace))

    contracts.validate_output(result, "test output")
    assert result["financial_resolution"] == {
        "currency": "BRL",
        "recommended_refund_brl": 0,
        "refund_lines": [],
    }


def test_solve_case_uses_conservative_fallback_after_two_invalid_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    invalid = {**sample_valid_output(), "case_id": "WRONG_CASE"}
    calls = 0

    async def fake_request_object(prompt: str) -> dict[str, Any]:
        nonlocal calls
        del prompt
        calls += 1
        return invalid

    monkeypatch.setattr("student_agent.workflow.request_object", fake_request_object)
    contracts = Contracts(PROJECT_ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(solve_case(sample_case(), FakeGateway(), trace))

    contracts.validate_output(result, "test output")
    assert calls == 2
    assert result["assessment"] == {
        "primary_issue": "insufficient_evidence",
        "secondary_issues": [],
        "case_status": "needs_investigation",
        "confidence": 0,
    }
    assert result["entity_resolution"]["resolved_order_ids"] == ["order-001"]
