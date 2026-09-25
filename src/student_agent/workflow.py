from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import Contracts
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .workers_ai import request_object

OUTPUT_CONTRACTS = Contracts(Path(__file__).resolve().parents[2] / "contracts" / "schemas")

OUTPUT_REQUIREMENTS = """
Return one object with exactly this top-level shape:
{
  "schema_version": "day09-l3b-output-v2",
  "case_id": string,
  "assessment": {
    "primary_issue": one of [canceled_order_paid, unavailable_order_paid,
      late_delivery_seller, late_delivery_logistics, valid_split_payment,
      payment_mismatch, duplicate_charge, refund_pending, refund_failed,
      unsupported_claim, insufficient_evidence],
    "secondary_issues": unique string array,
    "case_status": one of [action_required, no_action, needs_investigation],
    "confidence": number from 0 to 1
  },
  "affected_entities": {
    "order_ids": string array, "item_ids": string array,
    "seller_ids": string array, "payment_references": string array,
    "shipment_ids": string array
  },
  "claim_assessments": [{
    "claim_id": string,
    "verdict": one of [supported, unsupported, partially_supported,
      insufficient_evidence],
    "confidence": number from 0 to 1,
    "evidence_refs": evidence-ref string array
  }],
  "entity_resolution": {
    "status": one of [resolved, ambiguous, not_found],
    "resolved_order_ids": string array,
    "rejected_candidates": string array,
    "confidence": number from 0 to 1
  },
  "customer_context": {
    "customer_unique_id": string or null,
    "related_order_ids": string array
  },
  "shipment_analysis": {
    "verdict": one of [on_time, seller_delay, logistics_delay, lost, returned,
      conflicting, insufficient_evidence],
    "late_seller_ids": string array,
    "timeline_complete": boolean
  },
  "payment_analysis": {
    "verdict": one of [reconciled, capture_mismatch, duplicate_capture,
      refund_pending, refund_failed, refunded, insufficient_evidence],
    "captured_total_brl": non-negative number or null,
    "refunded_total_brl": non-negative number or null,
    "refundable_total_brl": non-negative number or null
  },
  "root_cause_analysis": {
    "ranked_causes": [{"cause_code": uppercase snake case, "rank": integer 1..5}],
    "responsible_parties": [{
      "party_type": one of [seller, platform, logistics_provider,
        payment_provider, customer, unknown],
      "party_id": string or null
    }]
  },
  "evidence_refs": evidence-ref string array,
  "data_conflicts": [{
    "field": string, "sources": at least two unique strings,
    "selected_source": string or null, "resolution_code": string
  }],
  "financial_resolution": {
    "currency": "BRL",
    "recommended_refund_brl": non-negative number,
    "refund_lines": [{
      "reason_code": string, "amount_brl": non-negative number,
      "entity_id": string or null
    }]
  },
  "resolution_actions": unique string array
}
Use only supplied IDs, amounts, facts, and evidence_refs. Include every input
claim_id exactly once. Treat requested_full_refund as a requested remedy, not a
primary_issue enum. Keep arrays empty when evidence does not support a value.
Do not expose reasoning. Output JSON only.
""".strip()

AGENTS = (
    "entity-agent",
    "customer-agent",
    "order-product-agent",
    "shipment-agent",
    "payment-refund-agent",
    "policy-conflict-agent",
)


def _evidence_found(evidence: dict[str, Any]) -> bool:
    data = evidence.get("data")
    if data is None:
        return False
    if isinstance(data, dict):
        if data.get("found") is False:
            return False
        if "order" in data and data["order"] is None:
            return False
    return bool(data)


async def collect_evidence(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    async def call(actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        key = (tool_name, tuple(sorted(arguments.items())))
        if key not in cache:
            try:
                evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
            except RuntimeError:
                evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
            cache[key] = evidence
            records.append(
                {
                    "tool_name": tool_name,
                    "actor": actor,
                    "arguments": arguments,
                    "evidence_ref": evidence["evidence_ref"],
                    "domain": evidence["domain"],
                    "data": evidence["data"],
                    "warnings": evidence.get("warnings", []),
                }
            )
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence["evidence_ref"]],
            )
        return cache[key]

    candidates = list(
        dict.fromkeys(
            [
                case["customer_request"].get("claimed_order_id"),
                *case.get("candidate_order_ids", []),
            ]
        )
    )
    order_evidence: dict[str, dict[str, Any]] = {}
    for order_id in (value for value in candidates if value):
        try:
            order_evidence[order_id] = await call(
                "entity-agent", "get_order", order_id=order_id
            )
        except RuntimeError as exc:
            failures.append(
                {
                    "tool_name": "get_order",
                    "arguments": {"order_id": order_id},
                    "error": str(exc),
                }
            )
            continue

    claimed_order_id = case["customer_request"].get("claimed_order_id")
    resolved_order_id = next(
        (
            order_id
            for order_id in candidates
            if order_id in order_evidence and _evidence_found(order_evidence[order_id])
        ),
        claimed_order_id,
    )

    customer_id = case.get("customer_unique_id_hint")
    if customer_id:
        try:
            await call(
                "customer-agent",
                "get_customer_history",
                customer_unique_id=customer_id,
            )
        except RuntimeError as exc:
            failures.append(
                {
                    "tool_name": "get_customer_history",
                    "arguments": {"customer_unique_id": customer_id},
                    "error": str(exc),
                }
            )
    await call(
        "policy-conflict-agent",
        "get_policy",
        policy_version=case["policy_version"],
    )

    if resolved_order_id:
        for actor, tool_name in (
            ("order-product-agent", "get_order_items"),
            ("order-product-agent", "get_product_context"),
            ("order-product-agent", "get_sellers"),
            ("shipment-agent", "get_shipment_summary"),
            ("payment-refund-agent", "get_order_payments"),
            ("payment-refund-agent", "get_payment_timeline"),
            ("payment-refund-agent", "get_refund_timeline"),
        ):
            try:
                await call(actor, tool_name, order_id=resolved_order_id)
            except RuntimeError as exc:
                failures.append(
                    {
                        "tool_name": tool_name,
                        "arguments": {"order_id": resolved_order_id},
                        "error": str(exc),
                    }
                )

    return {
        "case_id": case_id,
        "resolved_order_id": resolved_order_id,
        "records": records,
        "failures": failures,
        "evidence_refs": [record["evidence_ref"] for record in records],
    }


def _build_prompt(case: dict[str, Any], bundle: dict[str, Any]) -> str:
    payload = {
        "roles": [
            {
                "name": "entity-agent",
                "task": "resolve the true order and reject unsupported candidates",
            },
            {
                "name": "customer-agent",
                "task": "check customer identity and related order history",
            },
            {
                "name": "order-product-agent",
                "task": "identify affected items, sellers, and product context",
            },
            {
                "name": "shipment-agent",
                "task": "classify delivery outcome and responsible party",
            },
            {
                "name": "payment-refund-agent",
                "task": "reconcile captures, refunds, and refundable total",
            },
            {
                "name": "policy-conflict-agent",
                "task": "apply policy and resolve conflicting sources",
            },
            {
                "name": "verifier",
                "task": "check provenance, consistency, confidence, and final shape",
            },
        ],
        "case": case,
        "mcp_evidence": bundle["records"],
        "mcp_failures": bundle["failures"],
        "allowed_evidence_refs": bundle["evidence_refs"],
    }
    return (
        f"{OUTPUT_REQUIREMENTS}\n\n"
        "Coordinate all listed roles before producing the final object.\n"
        f"INVESTIGATION_PAYLOAD={json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _verify_output(
    output: dict[str, Any], case: dict[str, Any], allowed_refs: set[str]
) -> None:
    if output.get("schema_version") != "day09-l3b-output-v2":
        raise ValueError("wrong schema_version")
    if output.get("case_id") != case["case_id"]:
        raise ValueError("wrong case_id")
    submitted_refs = output.get("evidence_refs")
    if not isinstance(submitted_refs, list) or not submitted_refs:
        raise ValueError("evidence_refs must be a non-empty array")
    if not set(submitted_refs) <= allowed_refs:
        raise ValueError("output contains evidence outside the current case")

    expected_claims = {
        claim["claim_id"] for claim in case["customer_request"].get("claims", [])
    }
    assessments = output.get("claim_assessments")
    if not isinstance(assessments, list):
        raise ValueError("claim_assessments must be an array")
    actual_claims = {assessment.get("claim_id") for assessment in assessments}
    if actual_claims != expected_claims:
        raise ValueError("claim_assessments do not match input claims")
    for assessment in assessments:
        refs = assessment.get("evidence_refs")
        if not isinstance(refs, list) or not set(refs) <= allowed_refs:
            raise ValueError("claim assessment contains invalid evidence_refs")


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        normalized = [_normalize_value(item) for item in value]
        if all(isinstance(item, str) for item in normalized):
            return list(dict.fromkeys(normalized))
        return normalized
    return value


def _normalize_output(value: Any) -> Any:
    output = _normalize_value(value)
    if not isinstance(output, dict):
        return output
    output.setdefault("data_conflicts", [])
    output.setdefault("resolution_actions", [])
    if isinstance(output.get("assessment"), dict):
        output["assessment"].setdefault("secondary_issues", [])
    return output


def _repair_prompt(prompt: str, error: Exception) -> str:
    return (
        f"{prompt}\n\n"
        f"Your previous object failed validation: {error}. "
        "Return a corrected complete JSON object only."
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    for actor in AGENTS:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code="INVESTIGATE_CASE",
        )

    bundle = await collect_evidence(case, gateway, trace)
    refs_by_actor: dict[str, list[str]] = {}
    for record in bundle["records"]:
        refs_by_actor.setdefault(record["actor"], []).append(record["evidence_ref"])
    for actor, refs in refs_by_actor.items():
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            decision_code="EVIDENCE_READY",
            evidence_refs=refs,
        )

    policy_refs = refs_by_actor.get("policy-conflict-agent", [])
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-conflict-agent",
        target="coordinator",
        decision_code="POLICY_APPLIED",
        evidence_refs=policy_refs,
    )

    prompt = _build_prompt(case, bundle)
    allowed_refs = set(bundle["evidence_refs"])
    try:
        output = _normalize_output(await request_object(prompt))
        _verify_output(output, case, allowed_refs)
        OUTPUT_CONTRACTS.validate_output(output, f"model output for {case_id}")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as first_error:
        output = _normalize_output(await request_object(_repair_prompt(prompt, first_error)))
        _verify_output(output, case, allowed_refs)
        OUTPUT_CONTRACTS.validate_output(output, f"repaired model output for {case_id}")

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="VERIFY_OUTPUT",
        evidence_refs=output["evidence_refs"],
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="OUTPUT_VERIFIED",
        evidence_refs=output["evidence_refs"],
        attributes={
            "claim_count": len(output["claim_assessments"]),
            "evidence_count": len(output["evidence_refs"]),
        },
    )
    return output
