from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


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

    async def call(actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        key = (tool_name, tuple(sorted(arguments.items())))
        if key not in cache:
            evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
            cache[key] = evidence
            records.append(
                {
                    "tool_name": tool_name,
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
        except RuntimeError:
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
        await call(
            "customer-agent",
            "get_customer_history",
            customer_unique_id=customer_id,
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
            await call(actor, tool_name, order_id=resolved_order_id)

    return {
        "case_id": case_id,
        "resolved_order_id": resolved_order_id,
        "records": records,
        "evidence_refs": [record["evidence_ref"] for record in records],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3B coordinator and specialist-agent workflow here.

    Include entity resolution, conflict handling and evidence-efficient investigation.
    The starter kit intentionally does not generate invented fallback answers.
    """
    del case, gateway, trace
    raise NotImplementedError("Implement the L3B multi-agent workflow in solve_case()")
