"""Deterministic specialist decisions over MCP evidence, never over claim labels."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result.quantize(CENT) if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def ids(rows: list[dict], field: str) -> list[str]:
    return sorted({str(row[field]) for row in rows if row.get(field) is not None})


def conflict(field: str, *sources: str, selected: str | None = None) -> dict:
    return {
        "field": field,
        "sources": list(dict.fromkeys(sources)),
        "selected_source": selected,
        "resolution_code": "AUTHORITATIVE_SOURCE" if selected else "UNRESOLVED_SOURCE_CONFLICT",
    }


def unique_rows(rows: list[dict], key: str, source: str) -> tuple[list[dict], list[dict]]:
    """Deduplicate identical records; do not arbitrarily pick conflicting versions."""
    seen: dict[str, dict] = {}
    conflicts = []
    for index, row in enumerate(rows):
        identity = str(row.get(key, f"missing-{index}"))
        if identity in seen and seen[identity] != row:
            conflicts.append(conflict(key, f"{source}.first", f"{source}.conflicting"))
        else:
            seen[identity] = row
    return list(seen.values()), conflicts[:1]


def analyze_items(items: Any, products: Any) -> dict:
    if not isinstance(items, list) or not all(isinstance(row, dict) for row in items):
        return {"rows": [], "total": None, "freight": None, "conflicts": []}
    rows, conflicts = unique_rows(items, "order_item_id", "get_order_items")
    total = ZERO
    freight = ZERO
    for row in rows:
        price, delivery = money(row.get("price")), money(row.get("freight_value"))
        if price is None or delivery is None:
            total = freight = None
            break
        total += price + delivery
        freight += delivery
    if isinstance(products, list):
        product_ids = ids(products, "product_id")
        if set(ids(rows, "product_id")) != set(product_ids):
            conflicts.append(conflict("product_id", "get_order_items", "get_product_context"))
    if conflicts or not rows:
        total = freight = None
    return {"rows": rows, "total": total, "freight": freight, "conflicts": conflicts}


def analyze_shipment(order: dict, shipment: Any) -> dict:
    result = {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False}
    conflicts: list[dict] = []
    if not isinstance(shipment, dict):
        return {"analysis": result, "conflicts": conflicts, "shipment_ids": []}
    carrier = timestamp(shipment.get("delivered_carrier_at"))
    delivered = timestamp(shipment.get("delivered_customer_at"))
    estimated = timestamp(shipment.get("estimated_delivery_at"))
    purchased = timestamp(order.get("order_purchase_timestamp"))
    limits, repeated = unique_rows(
        shipment.get("shipping_limits", []), "order_item_id", "get_shipment_summary.shipping_limits"
    )
    conflicts.extend(repeated)
    for local, remote in (
        ("order_status", "order_status"),
        ("order_delivered_carrier_date", "delivered_carrier_at"),
        ("order_delivered_customer_date", "delivered_customer_at"),
        ("order_estimated_delivery_date", "estimated_delivery_at"),
    ):
        if order.get(local) != shipment.get(remote):
            conflicts.append(conflict(remote, "get_order", "get_shipment_summary"))
    events = shipment.get("events", [])
    late_events = [
        e
        for e in events
        if e.get("event_type") == "delivered_late" and e.get("status") == "confirmed"
    ]
    if delivered and estimated and delivered <= estimated and late_events:
        conflicts.append(
            conflict(
                "delivery_status", "get_shipment_summary.timestamps", "get_shipment_summary.events"
            )
        )
    event_dates = [timestamp(e.get("event_at")) for e in events]
    if (purchased and any(d and d < purchased for d in [carrier, delivered, *event_dates])) or (
        carrier and delivered and delivered < carrier
    ):
        conflicts.append(conflict("shipment_timeline", "get_order", "get_shipment_summary.events"))
    complete = bool(
        purchased
        and carrier
        and delivered
        and estimated
        and limits
        and all(timestamp(r.get("shipping_limit_at")) for r in limits)
    )
    result["timeline_complete"] = complete and not conflicts
    if conflicts:
        result["verdict"] = "conflicting"
    elif delivered and estimated and delivered <= estimated:
        result["verdict"] = "on_time"
    elif delivered and estimated and delivered > estimated and carrier and limits:
        if all(timestamp(r.get("shipping_limit_at")) for r in limits):
            late = [r for r in limits if carrier > timestamp(r["shipping_limit_at"])]
            result["late_seller_ids"] = ids(late, "seller_id")
            result["verdict"] = "seller_delay" if late else "logistics_delay"
    elif any(e.get("event_type") == "lost" and e.get("status") == "confirmed" for e in events):
        result["verdict"] = "lost"
    elif any(e.get("event_type") == "returned" and e.get("status") == "confirmed" for e in events):
        result["verdict"] = "returned"
    return {
        "analysis": result,
        "conflicts": conflicts,
        "shipment_ids": ids([shipment, *events], "shipment_id"),
    }


def analyze_payment(order: dict, timeline: Any, refund: Any, items: dict) -> dict:
    result = {
        "verdict": "insufficient_evidence",
        "captured_total_brl": None,
        "refunded_total_brl": None,
        "refundable_total_brl": None,
    }
    answer = {
        "analysis": result,
        "conflicts": [],
        "references": [],
        "split": False,
        "excess": None,
        "failed_amount": None,
        "refund_known": False,
    }
    if not isinstance(timeline, dict) or not isinstance(timeline.get("events"), list):
        return answer
    payments, conflicts = unique_rows(
        timeline.get("payments", []), "payment_sequential", "get_payment_timeline.payments"
    )
    events = list({json.dumps(e, sort_keys=True): e for e in timeline["events"]}.values())
    answer["references"] = ids([*payments, *events], "payment_reference")
    # A sequential number alone is not a provider payment reference.
    captured_events = [
        e
        for e in events
        if e.get("event_type") == "captured"
        and e.get("status") in {"confirmed", "completed", "succeeded"}
    ]
    amounts = [money(e.get("amount_brl")) for e in captured_events]
    purchased = timestamp(order.get("order_purchase_timestamp"))
    if purchased and any(
        (at := timestamp(e.get("event_at"))) and at < purchased for e in captured_events
    ):
        conflicts.append(conflict("capture_timeline", "get_order", "get_payment_timeline"))
    captured = sum(amounts, ZERO) if all(a is not None for a in amounts) else None
    if conflicts:
        captured = None
    refund_events = refund.get("events") if isinstance(refund, dict) else None
    refunded = None
    refund_verdict = None
    if isinstance(refund_events, list):
        answer["refund_known"] = True
        # Multiple lifecycle events for one refund must not be summed as separate refunds.
        latest: dict[str, dict] = {}
        for index, event in enumerate(refund_events):
            reference = event.get("refund_id") or event.get("refund_reference")
            key = str(reference) if reference else f"unreferenced-{index}"
            at = timestamp(event.get("event_at"))
            previous = latest.get(key)
            previous_at = timestamp(previous.get("event_at")) if previous else None
            if previous is not None and (at is None or previous_at is None):
                conflicts.append(
                    conflict(
                        "refund_timeline",
                        "get_refund_timeline.first",
                        "get_refund_timeline.conflicting",
                    )
                )
            if previous is None or (at and previous_at and at >= previous_at):
                latest[key] = event
        completed = [
            e
            for e in latest.values()
            if e.get("status") in {"confirmed", "completed", "succeeded", "refunded"}
        ]
        completed_amounts = [money(e.get("amount_brl")) for e in completed]
        refunded = (
            sum(completed_amounts, ZERO)
            if all(amount is not None for amount in completed_amounts)
            else None
        )
        if any(e.get("status") == "failed" for e in latest.values()):
            refund_verdict = "refund_failed"
            failed = [
                money(e.get("amount_brl")) for e in latest.values() if e.get("status") == "failed"
            ]
            answer["failed_amount"] = (
                sum(failed, ZERO) if all(a is not None for a in failed) else None
            )
        elif any(
            e.get("status") in {"pending", "requested", "processing"} for e in latest.values()
        ):
            refund_verdict = "refund_pending"
        elif refunded:
            refund_verdict = "refunded"
        if any(c["field"] == "refund_timeline" for c in conflicts):
            refunded = None
            refund_verdict = None
    if captured is not None and refunded is not None and refunded > captured:
        conflicts.append(conflict("refund_total", "get_payment_timeline", "get_refund_timeline"))
        refunded = None
    refundable = (
        max(ZERO, captured - refunded) if captured is not None and refunded is not None else None
    )
    total = items["total"]
    if refund_verdict:
        result["verdict"] = refund_verdict
    elif captured is not None and total is not None:
        if captured == total:
            result["verdict"] = "reconciled"
            answer["split"] = len(payments) > 1
        elif (
            captured > total
            and total > ZERO
            and (
                sum(a == total for a in amounts) >= 2
                or any(e.get("event_type") == "duplicate_capture" for e in events)
            )
        ):
            result["verdict"] = "duplicate_capture"
        else:
            result["verdict"] = "capture_mismatch"
        answer["excess"] = max(ZERO, captured - total)
    for field, value in (
        ("captured_total_brl", captured),
        ("refunded_total_brl", refunded),
        ("refundable_total_brl", refundable),
    ):
        result[field] = float(value) if value is not None else None
    answer["conflicts"] = conflicts
    return answer


def decide(
    order: dict,
    items: dict,
    shipment: dict,
    payment: dict,
    policy: Any,
    conflicts: list[dict],
    incomplete: bool,
) -> dict:
    """Apply public policy only after independently establishing the business issue."""
    pay, ship = payment["analysis"], shipment["analysis"]
    issue = "insufficient_evidence"
    if pay["verdict"] in {"refund_pending", "refund_failed"}:
        issue = pay["verdict"]
    elif conflicts or incomplete:
        pass
    elif order.get("order_status") in {"canceled", "unavailable"} and (
        pay["captured_total_brl"] is not None and pay["captured_total_brl"] > 0
    ):
        issue = f"{order['order_status']}_order_paid"
    elif pay["verdict"] == "duplicate_capture":
        issue = "duplicate_charge"
    elif pay["verdict"] == "capture_mismatch":
        issue = "payment_mismatch"
    elif ship["verdict"] in {"seller_delay", "logistics_delay"}:
        issue = (
            "late_delivery_seller"
            if ship["verdict"] == "seller_delay"
            else "late_delivery_logistics"
        )
    elif pay["verdict"] == "reconciled" and ship["verdict"] == "on_time":
        issue = "valid_split_payment" if payment["split"] else "unsupported_claim"
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    rule = rules.get(issue, {})
    amount = ZERO
    status = rule.get("case_status", "needs_investigation")
    action = rule.get("recommended_action", "request_manual_review")
    parties = []
    for party in rule.get("responsible_parties", []):
        if party.get("party_type") == "seller":
            sellers = ship["late_seller_ids"] or ids(items["rows"], "seller_id")
            parties.extend({"party_type": "seller", "party_id": seller} for seller in sellers)
        else:
            parties.append({"party_type": party.get("party_type", "unknown"), "party_id": None})
    entitlement = None
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        entitlement = money(pay["refundable_total_brl"])
    elif issue in {"late_delivery_seller", "late_delivery_logistics"}:
        entitlement = items["freight"]
    elif issue in {"duplicate_charge", "payment_mismatch"}:
        entitlement = payment["excess"]
    elif issue == "refund_failed":
        entitlement = payment["failed_amount"]
    policy_amount = money(rule.get("refund_brl"))
    balance = money(pay["refundable_total_brl"])
    if entitlement is not None and policy_amount is not None and balance is not None:
        amount = min(entitlement, policy_amount, balance)
    blocked = bool(conflicts or incomplete or issue == "insufficient_evidence" or not rule)
    if status == "action_required" and "refund" in action and amount == ZERO:
        blocked = True
    if blocked:
        amount, status, action = ZERO, "needs_investigation", "request_manual_review"
    confidence = 0.35 if issue == "insufficient_evidence" else 0.65 if blocked else 0.95
    return {
        "issue": issue,
        "status": status,
        "confidence": confidence,
        "amount": float(amount),
        "action": action,
        "parties": parties[:5],
        "blocked": blocked,
    }
