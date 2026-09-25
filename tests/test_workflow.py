from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.workflow import collect_evidence
from student_agent.workers_ai import WorkersAISettings, parse_model_object


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


def test_workers_ai_settings_require_all_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_AI_MODEL", "@cf/meta/test")

    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID"):
        WorkersAISettings.load()


def test_parse_model_object_accepts_plain_json() -> None:
    assert parse_model_object('{"case_id":"CASE_001"}') == {"case_id": "CASE_001"}


def test_parse_model_object_accepts_fenced_json() -> None:
    text = '```json\n{"case_id":"CASE_001"}\n```'
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
