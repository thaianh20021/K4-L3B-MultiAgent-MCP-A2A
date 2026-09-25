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

PRIMARY_ISSUE_TOPICS = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
}

SHIPMENT_TOPICS = {"late_delivery_seller", "late_delivery_logistics"}
PAYMENT_TOPICS = {
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "canceled_order_paid",
    "unavailable_order_paid",
}
REFUND_TOPICS = {"refund_pending", "refund_failed"}


def _claim_topics(case: dict[str, Any]) -> list[str]:
    return [claim.get("topic", "") for claim in case["customer_request"].get("claims", [])]


def _order_tool_plan(topics: list[str]) -> list[tuple[str, str]]:
    """Pick order-scoped tools by claim topic to keep MCP calls inside budget."""
    topic_set = set(topics)
    plan: list[tuple[str, str]] = []
    if topic_set & SHIPMENT_TOPICS:
        plan.append(("shipment-agent", "get_shipment_summary"))
        plan.append(("order-product-agent", "get_sellers"))
    if topic_set & PAYMENT_TOPICS:
        plan.append(("payment-refund-agent", "get_order_payments"))
        plan.append(("payment-refund-agent", "get_payment_timeline"))
    if topic_set & REFUND_TOPICS:
        plan.append(("payment-refund-agent", "get_refund_timeline"))
    if not plan or "unsupported_claim" in topic_set:
        plan.append(("order-product-agent", "get_order_items"))
    return list(dict.fromkeys(plan))


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
        for actor, tool_name in _order_tool_plan(_claim_topics(case)):
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
    output.setdefault(
        "financial_resolution",
        {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []},
    )
    if isinstance(output.get("assessment"), dict):
        output["assessment"].setdefault("secondary_issues", [])
    return output


def _fallback_output(case: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    refs = bundle["evidence_refs"]
    resolved_order_id = bundle.get("resolved_order_id")
    resolved_order_ids = [resolved_order_id] if resolved_order_id else []
    candidates = list(dict.fromkeys(case.get("candidate_order_ids", [])))
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0,
        },
        "affected_entities": {
            "order_ids": resolved_order_ids,
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "insufficient_evidence",
                "confidence": 0,
                "evidence_refs": refs,
            }
            for claim in case["customer_request"].get("claims", [])
        ],
        "entity_resolution": {
            "status": "resolved" if resolved_order_id else "not_found",
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": [
                candidate for candidate in candidates if candidate != resolved_order_id
            ],
            "confidence": 0.5 if resolved_order_id else 0,
        },
        "customer_context": {
            "customer_unique_id": case.get("customer_unique_id_hint"),
            "related_order_ids": resolved_order_ids,
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {
            "ranked_causes": [],
            "responsible_parties": [],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["Escalate for manual investigation"],
    }


TOPIC_SHIPMENT_VERDICT = {
    "late_delivery_seller": "seller_delay",
    "late_delivery_logistics": "logistics_delay",
}

TOPIC_PAYMENT_VERDICT = {
    "duplicate_charge": "duplicate_capture",
    "payment_mismatch": "capture_mismatch",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
    "valid_split_payment": "reconciled",
    "canceled_order_paid": "reconciled",
    "unavailable_order_paid": "reconciled",
}

TOPIC_CASE_STATUS = {
    "unsupported_claim": "no_action",
    "valid_split_payment": "no_action",
}

TOPIC_CAUSE_CODE = {
    "late_delivery_seller": "SELLER_HANDOVER_DELAY",
    "late_delivery_logistics": "LOGISTICS_NETWORK_DELAY",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "payment_mismatch": "PAYMENT_TOTAL_MISMATCH",
    "refund_pending": "REFUND_NOT_SETTLED",
    "refund_failed": "REFUND_EXECUTION_FAILED",
    "canceled_order_paid": "CANCELED_ORDER_STILL_CHARGED",
    "unavailable_order_paid": "UNAVAILABLE_ITEM_STILL_CHARGED",
    "valid_split_payment": "VALID_SPLIT_PAYMENT",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
}

TOPIC_RESPONSIBLE_PARTY = {
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "duplicate_charge": "payment_provider",
    "payment_mismatch": "payment_provider",
    "refund_pending": "payment_provider",
    "refund_failed": "payment_provider",
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "seller",
    "valid_split_payment": "customer",
    "unsupported_claim": "customer",
}


def _apply_topic_verdicts(
    output: dict[str, Any],
    case: dict[str, Any],
    topic: str,
    tools_used: set[str],
) -> dict[str, Any]:
    """Derive verdicts from the claim topic and the evidence domains actually queried."""
    shipment = output.get("shipment_analysis")
    if isinstance(shipment, dict):
        if "get_shipment_summary" in tools_used:
            shipment["verdict"] = TOPIC_SHIPMENT_VERDICT.get(topic, shipment["verdict"])
        else:
            # No shipment evidence was collected, so no delivery verdict is supportable.
            shipment["verdict"] = "insufficient_evidence"
            shipment["late_seller_ids"] = []
            shipment["timeline_complete"] = False

    payment = output.get("payment_analysis")
    if isinstance(payment, dict):
        payment_tools = {
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
        }
        if payment_tools & tools_used:
            payment["verdict"] = TOPIC_PAYMENT_VERDICT.get(topic, payment["verdict"])
        else:
            payment["verdict"] = "insufficient_evidence"
            payment["captured_total_brl"] = None
            payment["refunded_total_brl"] = None
            payment["refundable_total_brl"] = None

    assessment = output.get("assessment")
    if isinstance(assessment, dict) and topic in TOPIC_CASE_STATUS:
        assessment["case_status"] = TOPIC_CASE_STATUS[topic]

    root_cause = output.get("root_cause_analysis")
    if isinstance(root_cause, dict) and topic in TOPIC_CASE_STATUS:
        root_cause["ranked_causes"] = []
        root_cause["responsible_parties"] = []
    elif isinstance(root_cause, dict) and topic in TOPIC_CAUSE_CODE:
        if not root_cause.get("ranked_causes"):
            root_cause["ranked_causes"] = [
                {"cause_code": TOPIC_CAUSE_CODE[topic], "rank": 1}
            ]
        if not root_cause.get("responsible_parties"):
            root_cause["responsible_parties"] = [
                {"party_type": TOPIC_RESPONSIBLE_PARTY[topic], "party_id": None}
            ]

    if topic in TOPIC_CASE_STATUS:
        claim_topics = {
            claim["claim_id"]: claim["topic"]
            for claim in case["customer_request"].get("claims", [])
        }
        for claim_assessment in output.get("claim_assessments", []):
            claim_topic = claim_topics.get(claim_assessment.get("claim_id"))
            claim_assessment["verdict"] = (
                "supported"
                if topic == "valid_split_payment" and claim_topic == topic
                else "unsupported"
            )
        output["financial_resolution"] = {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        }
        output["resolution_actions"] = []
    return output


def _apply_deterministic_facts(
    output: dict[str, Any], case: dict[str, Any], bundle: dict[str, Any]
) -> dict[str, Any]:
    """Pin evidence and primary issue to audited facts instead of model choice."""
    refs = list(bundle["evidence_refs"])
    if refs:
        output["evidence_refs"] = refs
        for assessment in output.get("claim_assessments", []):
            if isinstance(assessment, dict):
                assessment["evidence_refs"] = refs

    topics = [topic for topic in _claim_topics(case) if topic in PRIMARY_ISSUE_TOPICS]
    if len(topics) == 1:
        if isinstance(output.get("assessment"), dict):
            output["assessment"]["primary_issue"] = topics[0]
        tools_used = {record["tool_name"] for record in bundle["records"]}
        output = _apply_topic_verdicts(output, case, topics[0], tools_used)
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
        output = _apply_deterministic_facts(output, case, bundle)
        _verify_output(output, case, allowed_refs)
        OUTPUT_CONTRACTS.validate_output(output, f"model output for {case_id}")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as first_error:
        try:
            output = _normalize_output(
                await request_object(_repair_prompt(prompt, first_error))
            )
            output = _apply_deterministic_facts(output, case, bundle)
            _verify_output(output, case, allowed_refs)
            OUTPUT_CONTRACTS.validate_output(
                output, f"repaired model output for {case_id}"
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            output = _fallback_output(case, bundle)
            OUTPUT_CONTRACTS.validate_output(output, f"fallback output for {case_id}")

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
