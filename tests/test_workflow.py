from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from student_agent.analysis import analyze_items, analyze_payment, analyze_shipment
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.trace import TraceWriter
from student_agent.verification import verify_output
from student_agent.workflow import DOMAINS, AgentMessage, CaseContext, solve_case

ROOT = Path(__file__).resolve().parents[1]


def sample() -> tuple[dict, dict]:
    order = {
        "order_id": "order-test",
        "customer_id": "customer-row-test",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-01-01T09:00:00-03:00",
        "order_delivered_carrier_date": "2018-01-02T09:00:00-03:00",
        "order_delivered_customer_date": "2018-01-04T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-01-05T09:00:00-03:00",
    }
    case = {
        "case_id": "CASE_TEST",
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": ["wrong-order", "order-test"],
        "customer_unique_id_hint": "customer-test",
        "customer_request": {"claims": [{"claim_id": "claim-test", "topic": "duplicate_charge"}]},
    }
    data = {
        "get_customer_history": {"customer_unique_id": "customer-test", "orders": [order]},
        "get_order": order,
        "get_order_items": [
            {
                "order_id": "order-test",
                "order_item_id": "item-test",
                "product_id": "product-test",
                "seller_id": "seller-test",
                "price": "80.00",
                "freight_value": "20.00",
            }
        ],
        "get_product_context": [{"product_id": "product-test", "order_item_id": "item-test"}],
        "get_shipment_summary": {
            "order_id": "order-test",
            "order_status": "delivered",
            "delivered_carrier_at": order["order_delivered_carrier_date"],
            "delivered_customer_at": order["order_delivered_customer_date"],
            "estimated_delivery_at": order["order_estimated_delivery_date"],
            "shipping_limits": [
                {
                    "order_item_id": "item-test",
                    "seller_id": "seller-test",
                    "shipping_limit_at": "2018-01-03T09:00:00-03:00",
                }
            ],
            "events": [],
        },
        "get_payment_timeline": {
            "order_id": "order-test",
            "payments": [
                {"payment_sequential": "1", "payment_value": "40.00"},
                {"payment_sequential": "2", "payment_value": "60.00"},
            ],
            "events": [
                {
                    "event_type": "captured",
                    "status": "confirmed",
                    "amount_brl": value,
                    "event_at": at,
                }
                for value, at in [
                    ("40.00", "2018-01-01T10:00:00-03:00"),
                    ("60.00", "2018-01-01T11:00:00-03:00"),
                ]
            ],
        },
        "get_refund_timeline": {"order_id": "order-test", "events": []},
        "get_policy": {
            "policy_version": "EC_POLICY_V2",
            "rules": {
                issue: {
                    "case_status": status,
                    "recommended_action": action,
                    "refund_brl": amount,
                    "responsible_parties": [],
                }
                for issue, status, action, amount in [
                    ("valid_split_payment", "no_action", "document_no_action", 0),
                    ("unsupported_claim", "no_action", "document_no_action", 0),
                    ("duplicate_charge", "action_required", "refund_duplicate_charge", 100),
                    ("refund_failed", "action_required", "retry_refund", 100),
                    ("refund_pending", "needs_investigation", "monitor_refund", 0),
                    ("late_delivery_seller", "action_required", "refund_freight", 20),
                    ("late_delivery_logistics", "action_required", "refund_freight", 20),
                    ("canceled_order_paid", "action_required", "issue_refund", 100),
                    ("unavailable_order_paid", "action_required", "issue_refund", 100),
                    ("payment_mismatch", "action_required", "reconcile_payment", 100),
                ]
            },
        },
    }
    return case, data


class FakeGateway:
    """Synthetic test fixture; never used to produce competition artifacts."""

    def __init__(self, data: dict) -> None:
        self.data = deepcopy(data)
        self.calls: list[tuple] = []

    async def list_tools(self):
        return list(self.data)

    async def call(self, name, *, case_id, **arguments):
        self.calls.append((name, case_id, arguments))
        value = self.data[name]
        if isinstance(value, Exception):
            raise value
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_testonly_{len(self.calls):020d}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": DOMAINS[name],
            "data": deepcopy(value),
        }


def trace_at(tmp_path):
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))


def run_case(tmp_path, case, data):
    gateway = FakeGateway(data)
    result = asyncio.run(solve_case(case, gateway, trace_at(tmp_path)))
    assert all(call[1] == case["case_id"] for call in gateway.calls)
    assert len(gateway.calls) <= 8
    return result


def test_resolves_without_claimed_order_and_does_not_echo_claim(tmp_path):
    case, data = sample()
    output = run_case(tmp_path, case, data)
    assert output["entity_resolution"]["resolved_order_ids"] == ["order-test"]
    assert output["entity_resolution"]["rejected_candidates"] == ["wrong-order"]
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


@pytest.mark.parametrize(
    "issue",
    [
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "late_delivery_seller",
        "late_delivery_logistics",
        "canceled_order_paid",
        "unavailable_order_paid",
        "payment_mismatch",
        "unsupported_claim",
    ],
)
def test_business_decisions_from_evidence(tmp_path, issue):
    case, data = sample()
    timeline = data["get_payment_timeline"]
    if issue in {"duplicate_charge", "payment_mismatch"}:
        values = ["100.00", "100.00"] if issue == "duplicate_charge" else ["40.00", "70.00"]
        for payment, event, value in zip(
            timeline["payments"], timeline["events"], values, strict=True
        ):
            payment["payment_value"] = event["amount_brl"] = value
    elif issue == "unsupported_claim":
        timeline["payments"] = [{"payment_sequential": "1", "payment_value": "100.00"}]
        timeline["events"] = [{**timeline["events"][0], "amount_brl": "100.00"}]
    elif issue.startswith("refund_"):
        data["get_refund_timeline"]["events"] = [
            {
                "refund_id": "refund-test",
                "event_at": "2018-01-06T09:00:00-03:00",
                "event_type": "refund_requested",
                "status": issue.removeprefix("refund_"),
                "amount_brl": "100.00",
            }
        ]
    elif issue.startswith("late_delivery"):
        data["get_order"]["order_delivered_customer_date"] = "2018-01-07T09:00:00-03:00"
        data["get_shipment_summary"]["delivered_customer_at"] = "2018-01-07T09:00:00-03:00"
        if issue.endswith("seller"):
            data["get_order"]["order_delivered_carrier_date"] = "2018-01-04T09:00:00-03:00"
            data["get_shipment_summary"]["delivered_carrier_at"] = "2018-01-04T09:00:00-03:00"
    elif issue.endswith("order_paid"):
        data["get_order"]["order_status"] = issue.split("_")[0]
        data["get_shipment_summary"]["order_status"] = issue.split("_")[0]
    output = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == issue
    if issue == "duplicate_charge":
        assert output["financial_resolution"]["recommended_refund_brl"] == 100
    if issue == "refund_failed":
        assert output["financial_resolution"]["recommended_refund_brl"] == 100


def test_conflicting_item_versions_are_not_added_or_selected(tmp_path):
    case, data = sample()
    data["get_order_items"].append({**data["get_order_items"][0], "price": "999.00"})
    output = run_case(tmp_path, case, data)
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["data_conflicts"][0]["selected_source"] is None
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_tool_failure_is_not_an_empty_refund_ledger(tmp_path):
    case, data = sample()
    data["get_refund_timeline"] = RuntimeError("unavailable")
    output = run_case(tmp_path, case, data)
    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_ambiguous_entity_does_not_query_order_specialists(tmp_path):
    case, data = sample()
    data["get_customer_history"]["orders"].append({**data["get_order"], "order_id": "wrong-order"})
    gateway = FakeGateway(data)
    output = asyncio.run(solve_case(case, gateway, trace_at(tmp_path)))
    assert output["entity_resolution"]["status"] == "ambiguous"
    assert [call[0] for call in gateway.calls] == ["get_customer_history", "get_policy"]


def test_case_scope_cache_permissions_retry_and_handoff(tmp_path):
    case, data = sample()
    gateway = FakeGateway(data)
    ctx = CaseContext(case, gateway, trace_at(tmp_path), set(data))

    async def check():
        args = {"customer_unique_id": "customer-test"}
        await ctx.fetch("entity-agent", "get_customer_history", **args)
        await ctx.fetch("entity-agent", "get_customer_history", **args)
        assert len(gateway.calls) == 1
        with pytest.raises(ValueError, match="no permission"):
            await ctx.fetch("payment-agent", "get_customer_history", **args)
        gateway.data["get_order"] = TimeoutError()
        assert await ctx.fetch("entity-agent", "get_order", order_id="order-test") is None
        assert len(gateway.calls) == 3
        await ctx.fetch("entity-agent", "get_order", order_id="order-test")
        assert len(gateway.calls) == 3
        with pytest.raises(ValueError, match="correlation"):
            ctx.receive(AgentMessage("OTHER_CASE", "entity-agent", "coordinator", {}, ()))

    asyncio.run(check())


def test_simultaneous_identical_calls_share_case_cache(tmp_path):
    case, data = sample()

    class SlowGateway(FakeGateway):
        async def call(self, *args, **kwargs):
            await asyncio.sleep(0.01)
            return await super().call(*args, **kwargs)

    gateway = SlowGateway(data)
    ctx = CaseContext(case, gateway, trace_at(tmp_path), set(data))

    async def check():
        await asyncio.gather(
            *[
                ctx.fetch(
                    "entity-agent", "get_customer_history", customer_unique_id="customer-test"
                )
                for _ in range(3)
            ]
        )
        assert len(gateway.calls) == 1

    asyncio.run(check())


def test_missing_record_lists_are_not_treated_as_empty_evidence(tmp_path):
    case, data = sample()
    data["get_refund_timeline"] = {"events": "invalid"}
    output = run_case(tmp_path, case, data)
    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert "missing_evidence" in output["assessment"]["secondary_issues"]


def test_resolve_single_history_order_without_candidate_ids(tmp_path):
    case, data = sample()
    case["candidate_order_ids"] = []
    output = run_case(tmp_path, case, data)
    assert output["entity_resolution"]["resolved_order_ids"] == ["order-test"]


def test_foreign_order_evidence_is_rejected(tmp_path):
    case, data = sample()
    data["get_order"]["order_id"] = "foreign-order"
    output = run_case(tmp_path, case, data)
    assert output["entity_resolution"]["status"] != "resolved"


def test_refund_lifecycle_uses_latest_event_per_reference():
    _, data = sample()
    refund = {
        "events": [
            {
                "refund_id": "r1",
                "status": "pending",
                "amount_brl": "40",
                "event_at": "2018-01-06T09:00:00-03:00",
            },
            {
                "refund_id": "r1",
                "status": "completed",
                "amount_brl": "40",
                "event_at": "2018-01-07T09:00:00-03:00",
            },
        ]
    }
    items = analyze_items(data["get_order_items"], None)
    result = analyze_payment(data["get_order"], data["get_payment_timeline"], refund, items)
    assert result["analysis"]["verdict"] == "refunded"
    assert result["analysis"]["refunded_total_brl"] == 40
    assert result["analysis"]["refundable_total_brl"] == 60


def test_late_event_conflicting_with_dates_is_explicit():
    _, data = sample()
    shipment = data["get_shipment_summary"]
    shipment["events"] = [{"event_type": "delivered_late", "status": "confirmed"}]
    result = analyze_shipment(data["get_order"], shipment)
    assert result["analysis"]["verdict"] == "conflicting"
    assert not result["analysis"]["timeline_complete"]


def test_verifier_rejects_unknown_evidence_and_excess_refund(tmp_path):
    case, data = sample()
    output = run_case(tmp_path, case, data)
    with pytest.raises(ValueError, match="evidence"):
        verify_output(output, case, set())
    output["financial_resolution"]["recommended_refund_brl"] = 900
    with pytest.raises(ValueError, match="refund lines"):
        verify_output(output, case, set(output["evidence_refs"]))


def test_gateway_sdk_v2_discovery_validation_and_cross_case_ownership():
    schema = {
        "type": "object",
        "required": ["case_id", "order_id"],
        "properties": {"case_id": {"type": "string"}, "order_id": {"type": "string"}},
    }
    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_" + "x" * 24,
        "domain": "order",
        "result_hash": "sha256:" + "0" * 64,
        "data": {},
    }

    class Session:
        pages = 0
        calls = 0

        async def list_tools(self, *, params=None):
            self.pages += 1
            return SimpleNamespace(
                tools=[SimpleNamespace(name="get_order", input_schema=schema)],
                next_cursor="page2" if params is None else None,
            )

        async def call_tool(self, name, arguments):
            self.calls += 1
            return SimpleNamespace(is_error=False, structured_content=evidence, content=[])

    async def check():
        session = Session()
        gateway = EvidenceGateway(session, Contracts(ROOT / "contracts/schemas"))
        assert await gateway.list_tools() == ["get_order"]
        assert await gateway.list_tools() == ["get_order"]
        assert session.pages == 2
        with pytest.raises(ValueError, match="not discovered"):
            await gateway.call("invented_tool", case_id="CASE_TEST")
        assert session.calls == 0
        assert await gateway.call("get_order", case_id="CASE_TEST", order_id="o") == evidence
        with pytest.raises(ValueError, match="another case"):
            await gateway.call("get_order", case_id="CASE_OTHER", order_id="o")

    asyncio.run(check())
