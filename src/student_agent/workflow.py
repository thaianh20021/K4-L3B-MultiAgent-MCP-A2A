from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# ---------------------------------------------------------------------------
# Tool discovery (README §4: "dùng tool discovery, không đoán tên tool").
# We resolve each logical capability against the gateway's live tool listing
# instead of assuming a name exists; a capability is simply skipped (with a
# warning) if the connected MCP Gateway does not expose it.
# ---------------------------------------------------------------------------
_CAPABILITY_TOOL = {
    "order": "get_order",
    "order_items": "get_order_items",
    "order_payments": "get_order_payments",
    "payment_timeline": "get_payment_timeline",
    "refund_timeline": "get_refund_timeline",
    "shipment_summary": "get_shipment_summary",
    "sellers": "get_sellers",
    "product_context": "get_product_context",
    "customer_history": "get_customer_history",
    "policy": "get_policy",
}

_TOOL_CACHE: dict[int, frozenset[str]] = {}

_PRIMARY_ISSUES = {
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed",
    "unsupported_claim", "insufficient_evidence",
}

# Priority used when more than one hypothesis is corroborated by evidence in
# the same case; earlier codes represent more urgent/actionable findings.
_ISSUE_PRIORITY = [
    "canceled_order_paid", "unavailable_order_paid", "duplicate_charge",
    "payment_mismatch", "refund_failed", "refund_pending",
    "late_delivery_seller", "late_delivery_logistics", "valid_split_payment",
    "unsupported_claim",
]

_REFUND_SUCCESS_STATUSES = {"confirmed", "completed", "succeeded", "refunded", "issued"}
_REFUND_FAILED_STATUSES = {"failed", "declined", "rejected"}
_REFUND_PENDING_STATUSES = {"pending", "requested", "processing"}

_MAX_CONFLICTS = 5


async def _discover_tools(gateway: EvidenceGateway) -> frozenset[str]:
    cached = _TOOL_CACHE.get(id(gateway))
    if cached is not None:
        return cached
    tools = frozenset(await gateway.list_tools())
    _TOOL_CACHE[id(gateway)] = tools
    return tools


# ---------------------------------------------------------------------------
# Per-case workspace: collected evidence refs, source conflicts, warnings.
# ---------------------------------------------------------------------------
@dataclass
class CaseWorkspace:
    case_id: str
    evidence_refs: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_conflict(
        self, field_name: str, sources: list[str], selected_source: str | None, resolution_code: str
    ) -> None:
        if len(self.conflicts) >= _MAX_CONFLICTS:
            return
        self.conflicts.append(
            {
                "field": field_name[:100],
                "sources": [s[:80] for s in dict.fromkeys(sources)][:5],
                "selected_source": selected_source[:80] if selected_source else None,
                "resolution_code": resolution_code[:80],
            }
        )

    def refs(self, limit: int = 30) -> list[str]:
        return list(dict.fromkeys(self.evidence_refs))[:limit]


async def _call_tool(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ws: CaseWorkspace,
    tools: frozenset[str],
    actor: str,
    capability: str,
    **arguments: str,
) -> dict[str, Any] | None:
    tool_name = _CAPABILITY_TOOL[capability]
    if tool_name not in tools:
        ws.warnings.append(f"tool_unavailable:{tool_name}")
        return None
    try:
        evidence = await gateway.call(tool_name, case_id=ws.case_id, **arguments)
    except (RuntimeError, ValueError) as exc:
        ws.warnings.append(f"{tool_name}_failed:{exc}"[:160])
        return None
    ref = evidence.get("evidence_ref")
    if ref:
        ws.evidence_refs.append(ref)
    trace.emit(
        case_id=ws.case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[ref] if ref else None,
    )
    return evidence


# ---------------------------------------------------------------------------
# Time / numeric helpers, all defensive against missing or malformed values.
# ---------------------------------------------------------------------------
def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_window(order_data: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """A generous [purchase-2d, max(estimated,delivered)+90d] sanity window.

    The sandbox MCP Gateway occasionally returns rows that nominally belong to
    the requested order_id but carry timestamps from a *different* order of
    the same customer (cross-order contamination used to test source-conflict
    handling). Anything falling outside this window is treated as suspect.
    """
    purchase = _parse_dt(order_data.get("order_purchase_timestamp"))
    estimated = _parse_dt(order_data.get("order_estimated_delivery_date"))
    delivered = _parse_dt(order_data.get("order_delivered_customer_date"))
    lower = purchase - timedelta(days=2) if purchase else None
    upper_candidates = [d for d in (estimated, delivered) if d]
    upper = max(upper_candidates) + timedelta(days=90) if upper_candidates else None
    return lower, upper


def _in_window(ts: datetime | None, lower: datetime | None, upper: datetime | None) -> bool:
    if ts is None:
        return True
    if lower and ts < lower:
        return False
    return not (upper and ts > upper)


def _filter_rows(
    rows: list[dict[str, Any]],
    date_key: str,
    lower: datetime | None,
    upper: datetime | None,
    tool_name: str,
    ws: CaseWorkspace,
    field_label: str,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in rows:
        ts = _parse_dt(row.get(date_key))
        (kept if _in_window(ts, lower, upper) else dropped).append(row)
    if dropped and kept:
        ws.add_conflict(
            field_label,
            [f"{tool_name}:in_window", f"{tool_name}:out_of_window"],
            f"{tool_name}:in_window",
            "temporal_consistency_filter",
        )
        return kept
    return rows


def _group_and_sum(
    rows: list[dict[str, Any]],
    amount_key: str,
    group_key_fn: Any,
    ws: CaseWorkspace,
    tool_name: str,
    corroborated_amounts: frozenset[float] = frozenset(),
) -> tuple[float, float, bool]:
    """Group rows by group_key_fn and sum one resolved amount per group.

    An exact repeated (key, amount) pair is treated as a genuine duplicate
    capture (the extra occurrences are returned separately). Rows sharing a
    key but disagreeing on amount are a source conflict: prefer whichever
    distinct value is corroborated by an independent, temporally-filtered
    source (payment timeline / refund timeline) and only fall back to a
    majority vote when no such corroboration is available.
    """
    groups: dict[Any, list[float]] = {}
    for row in rows:
        amount = _to_float(row.get(amount_key))
        if amount is None:
            continue
        groups.setdefault(group_key_fn(row), []).append(amount)

    total = 0.0
    duplicate_extra = 0.0
    mismatch = False
    for key, values in groups.items():
        distinct = sorted(set(values))
        if len(distinct) == 1:
            total += distinct[0]
            extra = len(values) - 1
            if extra > 0:
                duplicate_extra += distinct[0] * extra
        else:
            mismatch = True
            corroborated = [
                v for v in distinct if any(abs(v - c) < 0.01 for c in corroborated_amounts)
            ]
            resolution_code = "prefer_majority_value"
            if len(corroborated) == 1:
                preferred = corroborated[0]
                resolution_code = "prefer_corroborated_amount"
            else:
                preferred = max(distinct, key=values.count)
            total += preferred
            ws.add_conflict(
                f"payment_amount[{key}]",
                [f"{tool_name}:{v:.2f}" for v in distinct[:4]],
                f"{tool_name}:{preferred:.2f}",
                resolution_code,
            )
    return total, duplicate_extra, mismatch


# ---------------------------------------------------------------------------
# Entity / customer agent
# ---------------------------------------------------------------------------
@dataclass
class EntityResolution:
    status: str
    order_id: str | None
    resolved_order_ids: list[str]
    rejected_candidates: list[str]
    confidence: float
    order_data: dict[str, Any] | None
    customer_unique_id: str | None
    customer_order_ids: list[str]


async def _resolve_entity(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ws: CaseWorkspace,
    tools: frozenset[str],
) -> EntityResolution:
    request = case.get("customer_request") or {}
    claimed = request.get("claimed_order_id")
    candidates: list[str] = []
    if isinstance(claimed, str) and claimed:
        candidates.append(claimed)
    for candidate in case.get("candidate_order_ids") or []:
        if isinstance(candidate, str) and candidate and candidate not in candidates:
            candidates.append(candidate)
    candidates = candidates[:20]

    customer_hint = case.get("customer_unique_id_hint")
    customer_unique_id = customer_hint if isinstance(customer_hint, str) else None
    customer_order_ids: list[str] = []
    if customer_unique_id:
        evidence = await _call_tool(
            gateway, trace, ws, tools, "entity-agent", "customer_history",
            customer_unique_id=customer_unique_id,
        )
        if evidence:
            data = evidence.get("data") or {}
            customer_unique_id = data.get("customer_unique_id", customer_unique_id)
            for row in data.get("orders") or []:
                order_id = row.get("order_id")
                if isinstance(order_id, str) and order_id not in customer_order_ids:
                    customer_order_ids.append(order_id)

    resolved: list[str] = []
    rejected: list[str] = []
    order_data_by_id: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        evidence = await _call_tool(
            gateway, trace, ws, tools, "entity-agent", "order", order_id=candidate,
        )
        if evidence is None:
            rejected.append(candidate)
            continue
        data = evidence.get("data") or {}
        order_id = data.get("order_id") or candidate
        if customer_order_ids and order_id not in customer_order_ids:
            rejected.append(candidate)
            continue
        if order_id not in resolved:
            resolved.append(order_id)
            order_data_by_id[order_id] = data

    if len(resolved) == 1:
        status = "resolved"
        confidence = 0.95 if customer_order_ids else 0.7
    elif len(resolved) > 1:
        status = "ambiguous"
        confidence = 0.35
    else:
        status = "not_found"
        confidence = 0.1

    order_id = resolved[0] if len(resolved) == 1 else None
    return EntityResolution(
        status=status,
        order_id=order_id,
        resolved_order_ids=resolved[:20],
        rejected_candidates=rejected[:20],
        confidence=confidence,
        order_data=order_data_by_id.get(order_id) if order_id else None,
        customer_unique_id=customer_unique_id,
        customer_order_ids=customer_order_ids[:20],
    )


# ---------------------------------------------------------------------------
# Order / product agent
# ---------------------------------------------------------------------------
@dataclass
class OrderBundle:
    items: list[dict[str, Any]]
    sellers: list[dict[str, Any]]
    products: list[dict[str, Any]]


async def _investigate_order_context(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ws: CaseWorkspace,
    tools: frozenset[str],
    order_id: str,
    order_data: dict[str, Any],
) -> OrderBundle:
    lower, upper = _order_window(order_data)

    items_evidence = await _call_tool(
        gateway, trace, ws, tools, "order-agent", "order_items", order_id=order_id
    )
    items = _filter_rows(
        (items_evidence.get("data") or []) if items_evidence else [],
        "shipping_limit_date", lower, upper, "get_order_items", ws, "order_items",
    )

    sellers_evidence = await _call_tool(
        gateway, trace, ws, tools, "order-agent", "sellers", order_id=order_id
    )
    sellers = (sellers_evidence.get("data") or []) if sellers_evidence else []

    products: list[dict[str, Any]] = []
    scope = case.get("investigation_scope") or {}
    if scope.get("include_product_context", True):
        product_evidence = await _call_tool(
            gateway, trace, ws, tools, "order-agent", "product_context", order_id=order_id
        )
        products = (product_evidence.get("data") or []) if product_evidence else []

    return OrderBundle(items=items, sellers=sellers, products=products)


# ---------------------------------------------------------------------------
# Shipment agent
# ---------------------------------------------------------------------------
@dataclass
class ShipmentFindings:
    verdict: str
    late_seller_ids: list[str]
    timeline_complete: bool


async def _investigate_shipment(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ws: CaseWorkspace,
    tools: frozenset[str],
    order_id: str,
    order_data: dict[str, Any],
) -> ShipmentFindings:
    evidence = await _call_tool(
        gateway, trace, ws, tools, "shipment-agent", "shipment_summary", order_id=order_id
    )
    if evidence is None:
        return ShipmentFindings("insufficient_evidence", [], False)
    data = evidence.get("data") or {}

    order_status = order_data.get("order_status")
    shipment_status = data.get("order_status")
    if order_status and shipment_status and order_status != shipment_status:
        ws.add_conflict(
            "order_status", ["get_order", "get_shipment_summary"], "get_order",
            "prefer_order_source_of_record",
        )
    effective_status = order_status or shipment_status

    lower, upper = _order_window(order_data)
    limits = _filter_rows(
        data.get("shipping_limits") or [], "shipping_limit_at", lower, upper,
        "get_shipment_summary", ws, "shipping_limits",
    )
    events = _filter_rows(
        data.get("events") or [], "event_at", lower, upper,
        "get_shipment_summary", ws, "shipment_events",
    )

    delivered_carrier = _parse_dt(
        data.get("delivered_carrier_at") or order_data.get("order_delivered_carrier_date")
    )
    delivered_customer = _parse_dt(
        data.get("delivered_customer_at") or order_data.get("order_delivered_customer_date")
    )
    estimated = _parse_dt(
        data.get("estimated_delivery_at") or order_data.get("order_estimated_delivery_date")
    )

    late_sellers: set[str] = set()
    for limit in limits:
        limit_at = _parse_dt(limit.get("shipping_limit_at"))
        seller_id = limit.get("seller_id")
        if limit_at and delivered_carrier and seller_id and delivered_carrier > limit_at:
            late_sellers.add(seller_id)

    logistics_flagged = any(
        (e.get("event_type") in {"delivered_late", "delayed", "logistics_delay"})
        and e.get("actor") == "logistics_provider"
        for e in events
    )
    returned = any(e.get("event_type") in {"returned", "return_to_sender"} for e in events)
    timeline_complete = bool(delivered_carrier and delivered_customer and estimated)

    if effective_status == "canceled":
        verdict = "returned" if returned else "insufficient_evidence"
    elif returned:
        verdict = "returned"
    elif delivered_customer is None:
        opened_at = _parse_dt(case.get("opened_at"))
        if estimated and opened_at and opened_at > estimated + timedelta(days=30):
            verdict = "lost"
        else:
            verdict = "insufficient_evidence"
    elif estimated is None:
        verdict = "insufficient_evidence"
    elif delivered_customer <= estimated:
        verdict = "on_time"
    elif late_sellers and logistics_flagged:
        verdict = "conflicting"
    elif late_sellers:
        verdict = "seller_delay"
    else:
        verdict = "logistics_delay"

    return ShipmentFindings(
        verdict=verdict, late_seller_ids=sorted(late_sellers), timeline_complete=timeline_complete
    )


# ---------------------------------------------------------------------------
# Payment / refund agent
# ---------------------------------------------------------------------------
@dataclass
class PaymentFindings:
    verdict: str
    captured_total: float | None
    refunded_total: float
    refundable_total: float | None
    duplicate_extra: float
    outstanding_refund_amount: float
    distinct_payment_groups: int


async def _investigate_payment(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ws: CaseWorkspace,
    tools: frozenset[str],
    order_id: str,
    order_data: dict[str, Any],
) -> PaymentFindings:
    lower, upper = _order_window(order_data)

    payments_evidence = await _call_tool(
        gateway, trace, ws, tools, "payment-agent", "order_payments", order_id=order_id
    )
    payments_rows = (payments_evidence.get("data") or []) if payments_evidence else []

    timeline_evidence = await _call_tool(
        gateway, trace, ws, tools, "payment-agent", "payment_timeline", order_id=order_id
    )
    timeline_events: list[dict[str, Any]] = []
    if timeline_evidence:
        timeline_events = _filter_rows(
            (timeline_evidence.get("data") or {}).get("events") or [],
            "event_at", lower, upper, "get_payment_timeline", ws, "payment_events",
        )

    refund_evidence = await _call_tool(
        gateway, trace, ws, tools, "payment-agent", "refund_timeline", order_id=order_id
    )
    refund_events: list[dict[str, Any]] = []
    if refund_evidence:
        refund_events = _filter_rows(
            (refund_evidence.get("data") or {}).get("events") or [],
            "event_at", lower, upper, "get_refund_timeline", ws, "refund_events",
        )

    captured_events = [
        e for e in timeline_events if (e.get("event_type") or "").lower() == "captured"
    ]
    corroborated_amounts = frozenset(
        amount
        for amount in (
            _to_float(e.get("amount_brl")) for e in (*captured_events, *refund_events)
        )
        if amount is not None
    )

    payments_total, duplicate_extra, _mismatch = _group_and_sum(
        payments_rows, "payment_value",
        lambda r: (r.get("payment_sequential"), r.get("payment_type")),
        ws, "get_order_payments", corroborated_amounts=corroborated_amounts,
    )
    distinct_payment_groups = len(
        {
            (r.get("payment_sequential"), r.get("payment_type"))
            for r in payments_rows
            if r.get("payment_sequential")
        }
    )
    timeline_total, timeline_duplicate_extra, _mismatch2 = _group_and_sum(
        captured_events, "amount_brl", lambda r: r.get("event_at"), ws, "get_payment_timeline",
    )

    if payments_rows and captured_events:
        combined_payments = payments_total + duplicate_extra
        combined_timeline = timeline_total + timeline_duplicate_extra
        if abs(combined_payments - combined_timeline) > 0.01:
            ws.add_conflict(
                "captured_total_brl",
                [
                    f"get_order_payments:{combined_payments:.2f}",
                    f"get_payment_timeline:{combined_timeline:.2f}",
                ],
                f"get_payment_timeline:{combined_timeline:.2f}",
                "prefer_authoritative_payment_timeline",
            )
            captured_total = combined_timeline
        else:
            captured_total = combined_payments
    elif captured_events:
        captured_total = timeline_total + timeline_duplicate_extra
    elif payments_rows:
        captured_total = payments_total + duplicate_extra
    else:
        captured_total = None

    refunded_total = 0.0
    outstanding = 0.0
    refund_state: str | None = None
    for event in refund_events:
        amount = _to_float(event.get("amount_brl"))
        status = (event.get("status") or "").lower()
        if amount is None:
            continue
        if status in _REFUND_SUCCESS_STATUSES:
            refunded_total += amount
            refund_state = refund_state or "refunded"
        elif status in _REFUND_FAILED_STATUSES:
            outstanding += amount
            refund_state = "refund_failed"
        elif status in _REFUND_PENDING_STATUSES:
            outstanding += amount
            refund_state = refund_state or "refund_pending"

    refundable = None
    if captured_total is not None:
        refundable = max(round(captured_total - refunded_total, 2), 0.0)

    # get_payment_timeline is temporally filterable and described as
    # authoritative, so once it is available it overrides a raw mismatch
    # signal from get_order_payments (which carries no per-row timestamp and
    # can otherwise be fooled by cross-order contamination).
    mismatch_signal = _mismatch2 if captured_events else _mismatch

    if duplicate_extra > 0:
        verdict = "duplicate_capture"
    elif refund_state == "refund_failed":
        verdict = "refund_failed"
    elif refund_state == "refund_pending":
        verdict = "refund_pending"
    elif refund_state == "refunded":
        verdict = "refunded"
    elif mismatch_signal and refund_state is None:
        verdict = "capture_mismatch"
    elif captured_total is not None:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"

    return PaymentFindings(
        verdict=verdict,
        captured_total=round(captured_total, 2) if captured_total is not None else None,
        refunded_total=round(refunded_total, 2),
        refundable_total=refundable,
        duplicate_extra=round(duplicate_extra, 2),
        outstanding_refund_amount=round(outstanding, 2),
        distinct_payment_groups=distinct_payment_groups,
    )


# ---------------------------------------------------------------------------
# Policy agent
# ---------------------------------------------------------------------------
async def _load_policy(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ws: CaseWorkspace,
    tools: frozenset[str],
) -> dict[str, Any]:
    policy_version = case.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version:
        return {}
    evidence = await _call_tool(
        gateway, trace, ws, tools, "policy-agent", "policy", policy_version=policy_version
    )
    if not evidence:
        return {}
    return (evidence.get("data") or {}).get("rules") or {}


# ---------------------------------------------------------------------------
# Conflict resolver / root-cause reasoning (no MCP calls: pure synthesis of
# the evidence already gathered by the specialist agents above).
# ---------------------------------------------------------------------------
def _claims(case: dict[str, Any]) -> list[dict[str, str]]:
    claims = (case.get("customer_request") or {}).get("claims") or []
    return [c for c in claims if isinstance(c, dict) and c.get("claim_id") and c.get("topic")]


def _determine_primary_issue(
    entity: EntityResolution,
    order_data: dict[str, Any],
    shipment: ShipmentFindings,
    payment: PaymentFindings,
) -> tuple[str, set[str]]:
    corroborated: set[str] = set()
    if shipment.verdict == "seller_delay":
        corroborated.add("late_delivery_seller")
    if shipment.verdict == "logistics_delay":
        corroborated.add("late_delivery_logistics")
    if payment.verdict == "duplicate_capture":
        corroborated.add("duplicate_charge")
    if payment.verdict == "capture_mismatch":
        corroborated.add("payment_mismatch")
    if payment.verdict == "refund_pending":
        corroborated.add("refund_pending")
    if payment.verdict == "refund_failed":
        corroborated.add("refund_failed")
    order_status = order_data.get("order_status")
    if order_status == "canceled" and payment.captured_total:
        corroborated.add("canceled_order_paid")
    if order_status == "unavailable" and payment.captured_total:
        corroborated.add("unavailable_order_paid")
    if (
        payment.verdict == "reconciled"
        and shipment.verdict == "on_time"
        and payment.distinct_payment_groups >= 2
        and not corroborated
    ):
        corroborated.add("valid_split_payment")

    for candidate in _ISSUE_PRIORITY:
        if candidate in corroborated:
            return candidate, corroborated

    if entity.status != "resolved":
        return "insufficient_evidence", corroborated
    if shipment.verdict == "insufficient_evidence" and payment.verdict == "insufficient_evidence":
        return "insufficient_evidence", corroborated
    return "unsupported_claim", corroborated


def _build_resolution(
    case: dict[str, Any],
    entity: EntityResolution,
    order_bundle: OrderBundle,
    shipment: ShipmentFindings,
    payment: PaymentFindings,
    policy_rules: dict[str, Any],
    ws: CaseWorkspace,
) -> dict[str, Any]:
    order_data = entity.order_data or {}
    primary_issue, corroborated = _determine_primary_issue(entity, order_data, shipment, payment)
    claims = _claims(case)
    claim_topics = {c["topic"] for c in claims if c["topic"] in _PRIMARY_ISSUES}
    secondary_issues = sorted((corroborated | claim_topics) - {primary_issue})[:10]

    rule = policy_rules.get(primary_issue) if primary_issue != "insufficient_evidence" else None
    if rule:
        case_status = rule.get("case_status", "needs_investigation")
        recommended_action = rule.get("recommended_action")
        responsible_parties = []
        for party in rule.get("responsible_parties") or []:
            party_type = party.get("party_type", "unknown")
            party_id = party.get("party_id")
            if party_type == "seller" and shipment.late_seller_ids:
                party_id = shipment.late_seller_ids[0]
            responsible_parties.append({"party_type": party_type, "party_id": party_id})
        if not responsible_parties:
            responsible_parties = [{"party_type": "unknown", "party_id": None}]
    elif primary_issue == "insufficient_evidence":
        case_status = "needs_investigation"
        recommended_action = "request_additional_evidence"
        responsible_parties = [{"party_type": "unknown", "party_id": None}]
    else:
        case_status = "no_action"
        recommended_action = "document_no_action"
        responsible_parties = [{"party_type": "unknown", "party_id": None}]

    refund_lines: list[dict[str, Any]] = []
    recommended_refund = 0.0
    if primary_issue == "duplicate_charge" and payment.duplicate_extra:
        recommended_refund = payment.duplicate_extra
        refund_lines.append({
            "reason_code": "duplicate_charge_reversal",
            "amount_brl": round(payment.duplicate_extra, 2),
            "entity_id": entity.order_id,
        })
    elif primary_issue in {"refund_failed", "refund_pending"} and payment.outstanding_refund_amount:
        recommended_refund = payment.outstanding_refund_amount
        refund_lines.append({
            "reason_code": f"{primary_issue}_amount_due",
            "amount_brl": round(payment.outstanding_refund_amount, 2),
            "entity_id": entity.order_id,
        })
    elif (
        primary_issue in {"canceled_order_paid", "unavailable_order_paid"}
        and payment.captured_total
    ):
        recommended_refund = payment.captured_total
        refund_lines.append({
            "reason_code": "full_order_refund",
            "amount_brl": round(payment.captured_total, 2),
            "entity_id": entity.order_id,
        })
    elif primary_issue in {"late_delivery_seller", "late_delivery_logistics"}:
        freight_total = sum(
            v for v in (_to_float(item.get("freight_value")) for item in order_bundle.items) if v
        )
        if freight_total:
            recommended_refund = freight_total
            responsible_id = (
                shipment.late_seller_ids[0] if shipment.late_seller_ids else entity.order_id
            )
            refund_lines.append({
                "reason_code": "late_delivery_freight_refund",
                "amount_brl": round(freight_total, 2),
                "entity_id": responsible_id,
            })
    recommended_refund = round(max(recommended_refund, 0.0), 2)

    resolution_actions: list[str] = []
    if recommended_action:
        resolution_actions.append(recommended_action)
    if case_status == "action_required":
        resolution_actions.append(
            "notify_customer_refund_issued" if refund_lines else "notify_customer"
        )
    elif case_status == "needs_investigation":
        resolution_actions.append("escalate_for_manual_review")
    else:
        resolution_actions.append("document_no_action")
    resolution_actions = list(dict.fromkeys(resolution_actions))[:8]

    claim_assessments = []
    for claim in claims[:5]:
        topic = claim["topic"]
        if topic == primary_issue:
            verdict, confidence = "supported", 0.85
        elif topic in secondary_issues:
            verdict, confidence = "partially_supported", 0.6
        elif topic == "requested_full_refund":
            if case_status == "action_required":
                verdict, confidence = "supported", 0.75
            elif case_status == "needs_investigation":
                verdict, confidence = "insufficient_evidence", 0.3
            else:
                verdict, confidence = "unsupported", 0.7
        elif topic in _PRIMARY_ISSUES:
            verdict, confidence = "unsupported", 0.7
        else:
            verdict, confidence = "insufficient_evidence", 0.2
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": round(confidence, 2),
                "evidence_refs": ws.refs(),
            }
        )

    cause_code = primary_issue.upper()
    ranked_causes = [{"cause_code": cause_code, "rank": 1}]
    for rank, extra in enumerate(secondary_issues[:4], start=2):
        ranked_causes.append({"cause_code": extra.upper(), "rank": rank})

    base_confidence = 0.55
    if entity.status == "resolved":
        base_confidence += 0.15
    if shipment.timeline_complete:
        base_confidence += 0.1
    if payment.verdict != "insufficient_evidence":
        base_confidence += 0.1
    base_confidence -= 0.05 * min(len(ws.conflicts), 4)
    base_confidence -= 0.05 * min(len(ws.warnings), 4)
    if primary_issue == "insufficient_evidence":
        base_confidence = min(base_confidence, 0.35)
    confidence = round(min(max(base_confidence, 0.05), 0.97), 2)

    return {
        "primary_issue": primary_issue,
        "secondary_issues": secondary_issues,
        "case_status": case_status,
        "confidence": confidence,
        "responsible_parties": responsible_parties,
        "ranked_causes": ranked_causes,
        "recommended_refund_brl": recommended_refund,
        "refund_lines": refund_lines[:10],
        "resolution_actions": resolution_actions,
        "claim_assessments": claim_assessments,
    }


def _verify(
    entity: EntityResolution, shipment: ShipmentFindings, resolution: dict[str, Any]
) -> tuple[str, list[str]]:
    issues: list[str] = []
    if resolution["case_status"] == "action_required" and not resolution["refund_lines"]:
        issues.append("action_required_without_refund_line")
    if entity.status != "resolved" and resolution["primary_issue"] != "insufficient_evidence":
        issues.append("primary_issue_without_resolved_entity")
    if shipment.late_seller_ids and shipment.verdict not in {"seller_delay", "conflicting"}:
        issues.append("late_seller_ids_without_seller_delay_verdict")
    if issues:
        resolution["case_status"] = "needs_investigation"
        resolution["confidence"] = round(max(resolution["confidence"] - 0.2, 0.05), 2)
    decision_code = "checks_passed" if not issues else "checks_flagged"
    return decision_code, issues


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------
def _affected_entities(
    entity: EntityResolution, order_bundle: OrderBundle | None, shipment: ShipmentFindings | None
) -> dict[str, list[str]]:
    order_ids = [entity.order_id] if entity.order_id else []
    item_ids: list[str] = []
    seller_ids: list[str] = []
    payment_references: list[str] = []
    shipment_ids: list[str] = []

    if order_bundle:
        for item in order_bundle.items:
            item_id = item.get("order_item_id")
            if item_id and item_id not in item_ids:
                item_ids.append(item_id)
            seller_id = item.get("seller_id")
            if seller_id and seller_id not in seller_ids:
                seller_ids.append(seller_id)
        for seller in order_bundle.sellers:
            seller_id = seller.get("seller_id")
            if seller_id and seller_id not in seller_ids:
                seller_ids.append(seller_id)

    if shipment:
        for seller_id in shipment.late_seller_ids:
            if seller_id not in seller_ids:
                seller_ids.append(seller_id)
        if entity.order_id:
            shipment_ids.append(f"{entity.order_id}#shipment")

    if entity.order_id:
        payment_references.append(f"{entity.order_id}#payments")

    return {
        "order_ids": order_ids[:20],
        "item_ids": item_ids[:20],
        "seller_ids": seller_ids[:20],
        "payment_references": payment_references[:20],
        "shipment_ids": shipment_ids[:20],
    }


def _empty_output(
    case: dict[str, Any], entity: EntityResolution, ws: CaseWorkspace
) -> dict[str, Any]:
    case_id = case["case_id"]
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": round(min(entity.confidence, 0.3), 2),
        },
        "affected_entities": _affected_entities(entity, None, None),
        "entity_resolution": {
            "status": entity.status,
            "resolved_order_ids": entity.resolved_order_ids,
            "rejected_candidates": entity.rejected_candidates,
            "confidence": entity.confidence,
        },
        "customer_context": {
            "customer_unique_id": entity.customer_unique_id,
            "related_order_ids": entity.customer_order_ids,
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
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": ws.refs(),
        "data_conflicts": ws.conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["request_additional_evidence"],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3B coordinator: entity resolution -> specialist investigation -> conflict
    resolution -> verification -> output.

    Design notes (see ARCHITECTURE.md for the full record):
    - ``customer_request.message`` is free-text customer input and is never
      parsed for instructions; only structured fields (claims, candidate
      order ids, hints) drive control flow, per the independent-verification
      requirement embedded in several sample cases.
    - Every MCP call is scoped to this case_id and evidence_refs are recorded
      verbatim from the gateway response, never fabricated or reused.
    """
    case_id = case["case_id"]
    tools = await _discover_tools(gateway)
    ws = CaseWorkspace(case_id=case_id)

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator",
        target="entity-agent", decision_code="resolve_entity",
    )
    entity = await _resolve_entity(case, gateway, trace, ws, tools)
    trace.emit(
        case_id=case_id, event_type="handoff", actor="entity-agent", target="coordinator",
        decision_code=entity.status,
        attributes={"resolved_count": len(entity.resolved_order_ids)},
    )

    if entity.status != "resolved" or entity.order_id is None or entity.order_data is None:
        trace.emit(
            case_id=case_id, event_type="task_assigned", actor="coordinator",
            target="verifier", decision_code="verify_unresolved_entity",
        )
        trace.emit(
            case_id=case_id, event_type="verification_completed", actor="verifier",
            decision_code="entity_not_resolved", attributes={"status": entity.status},
        )
        trace.emit(
            case_id=case_id, event_type="handoff", actor="verifier", target="coordinator",
            decision_code="entity_not_resolved",
        )
        output = _empty_output(case, entity, ws)
        return output

    order_id = entity.order_id
    order_data = entity.order_data

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator",
        target="order-agent", decision_code="investigate_order",
    )
    order_bundle = await _investigate_order_context(
        case, gateway, trace, ws, tools, order_id, order_data
    )
    trace.emit(
        case_id=case_id, event_type="handoff", actor="order-agent", target="coordinator",
        decision_code="order_context_ready",
    )

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator",
        target="shipment-agent", decision_code="investigate_shipment",
    )
    shipment = await _investigate_shipment(case, gateway, trace, ws, tools, order_id, order_data)
    trace.emit(
        case_id=case_id, event_type="handoff", actor="shipment-agent", target="coordinator",
        decision_code=shipment.verdict,
    )

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator",
        target="payment-agent", decision_code="investigate_payment",
    )
    payment = await _investigate_payment(case, gateway, trace, ws, tools, order_id, order_data)
    trace.emit(
        case_id=case_id, event_type="handoff", actor="payment-agent", target="coordinator",
        decision_code=payment.verdict,
    )

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator",
        target="policy-agent", decision_code="load_policy",
    )
    policy_rules = await _load_policy(case, gateway, trace, ws, tools)
    trace.emit(
        case_id=case_id, event_type="handoff", actor="policy-agent", target="coordinator",
        decision_code="policy_loaded",
    )

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator",
        target="conflict-resolver", decision_code="resolve_root_cause",
    )
    resolution = _build_resolution(case, entity, order_bundle, shipment, payment, policy_rules, ws)
    trace.emit(
        case_id=case_id, event_type="policy_decided", actor="conflict-resolver",
        decision_code=resolution["primary_issue"],
        attributes={"case_status": resolution["case_status"], "conflicts": len(ws.conflicts)},
    )
    trace.emit(
        case_id=case_id, event_type="handoff", actor="conflict-resolver", target="coordinator",
        decision_code=resolution["primary_issue"],
    )

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator",
        target="verifier", decision_code="verify_case",
    )
    decision_code, issues = _verify(entity, shipment, resolution)
    trace.emit(
        case_id=case_id, event_type="verification_completed", actor="verifier",
        decision_code=decision_code, attributes={"issue_count": len(issues)},
    )
    trace.emit(
        case_id=case_id, event_type="handoff", actor="verifier", target="coordinator",
        decision_code=decision_code,
    )

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": resolution["primary_issue"],
            "secondary_issues": resolution["secondary_issues"],
            "case_status": resolution["case_status"],
            "confidence": resolution["confidence"],
        },
        "affected_entities": _affected_entities(entity, order_bundle, shipment),
        "claim_assessments": resolution["claim_assessments"],
        "entity_resolution": {
            "status": entity.status,
            "resolved_order_ids": entity.resolved_order_ids,
            "rejected_candidates": entity.rejected_candidates,
            "confidence": entity.confidence,
        },
        "customer_context": {
            "customer_unique_id": entity.customer_unique_id,
            "related_order_ids": entity.customer_order_ids,
        },
        "shipment_analysis": {
            "verdict": shipment.verdict,
            "late_seller_ids": shipment.late_seller_ids[:20],
            "timeline_complete": shipment.timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment.verdict,
            "captured_total_brl": payment.captured_total,
            "refunded_total_brl": payment.refunded_total,
            "refundable_total_brl": payment.refundable_total,
        },
        "root_cause_analysis": {
            "ranked_causes": resolution["ranked_causes"],
            "responsible_parties": resolution["responsible_parties"],
        },
        "evidence_refs": ws.refs(),
        "data_conflicts": ws.conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": resolution["recommended_refund_brl"],
            "refund_lines": resolution["refund_lines"],
        },
        "resolution_actions": resolution["resolution_actions"],
    }
    return output
