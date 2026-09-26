from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx2

from . import OUTPUT_SCHEMA_VERSION
from .analysis import analyze_items, analyze_payment, analyze_shipment, conflict, decide, ids
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verification import verify_output

PERMISSIONS = {
    "entity-agent": {"get_customer_history", "get_order"},
    "order-agent": {"get_order_items", "get_product_context"},
    "shipment-agent": {"get_shipment_summary"},
    "payment-agent": {"get_payment_timeline", "get_refund_timeline"},
    "policy-agent": {"get_policy"},
}
DOMAINS = {
    "get_customer_history": "customer",
    "get_order": "order",
    "get_order_items": "item",
    "get_product_context": "product",
    "get_shipment_summary": "shipment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_policy": "policy",
}


@dataclass(frozen=True)
class AgentMessage:
    case_id: str
    sender: str
    recipient: str
    payload: dict[str, Any]
    evidence_refs: tuple[str, ...]


class CaseContext:
    """One short-lived context owns all evidence and budgets for one solve_case call."""

    def __init__(
        self,
        case: dict,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        tools: set[str],
        *,
        call_budget: int = 12,
        timeout: float = 20.0,
    ) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.gateway, self.trace, self.tools = gateway, trace, tools
        self.call_budget, self.timeout = call_budget, timeout
        self.calls = 0
        self.cache: dict[str, dict | None] = {}
        self.evidence: dict[str, dict] = {}
        self.consumed: dict[str, set[str]] = {}
        self.failures: list[str] = []
        self.semaphore = asyncio.Semaphore(3)
        self._locks: dict[str, asyncio.Lock] = {}

    def emit(self, event_type: str, actor: str, **kwargs: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **kwargs)

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Any:
        if tool not in PERMISSIONS.get(actor, set()):
            raise ValueError(f"{actor} has no permission for {tool}")
        key = json.dumps([tool, arguments], sort_keys=True)
        async with self._locks.setdefault(key, asyncio.Lock()):
            return await self._fetch(actor, tool, key, arguments)

    async def _fetch(self, actor: str, tool: str, key: str, arguments: dict) -> Any:
        if key not in self.cache:
            evidence = None
            failure = "TOOL_UNAVAILABLE"
            if tool in self.tools:
                async with self.semaphore:
                    for attempt in range(2):
                        if self.calls >= self.call_budget:
                            failure = "CALL_BUDGET_EXHAUSTED"
                            break
                        self.calls += 1
                        try:
                            async with asyncio.timeout(self.timeout):
                                evidence = await self.gateway.call(
                                    tool, case_id=self.case_id, **arguments
                                )
                            self.trace.contracts.validate_evidence(evidence)
                            if evidence["domain"] != DOMAINS[tool]:
                                raise ValueError("MCP domain mismatch")
                            self._check_data(tool, evidence["data"])
                            self._check_scope(evidence["data"], arguments)
                            break
                        except (TimeoutError, httpx2.TransportError):
                            evidence = None
                            failure = "MCP_TIMEOUT" if attempt == 1 else "MCP_RETRY"
                            self.emit(
                                "handoff",
                                actor,
                                target="coordinator",
                                decision_code=failure,
                                tool_name=tool,
                                attributes={"attempt": attempt + 1},
                            )
                        except (RuntimeError, ValueError, TypeError):
                            evidence = None
                            failure = "MCP_INVALID_OR_UNAVAILABLE"
                            break
            self.cache[key] = evidence
            if evidence is None:
                self.failures.append(tool)
                self.emit(
                    "handoff", actor, target="coordinator", decision_code=failure, tool_name=tool
                )
        evidence = self.cache[key]
        if evidence is None:
            return None
        ref = evidence["evidence_ref"]
        if ref in self.evidence and self.evidence[ref] != evidence:
            raise ValueError("An evidence_ref changed within the case")
        self.evidence[ref] = evidence
        self.consumed.setdefault(actor, set()).add(ref)
        self.emit("tool_result_consumed", actor, tool_name=tool, evidence_refs=[ref])
        return deepcopy(evidence["data"])

    @staticmethod
    def _check_data(tool: str, data: Any) -> None:
        def records(value: Any) -> bool:
            return isinstance(value, list) and all(isinstance(row, dict) for row in value)

        if tool in {"get_order_items", "get_product_context"}:
            if not records(data):
                raise ValueError("MCP data must be a list of records")
        elif not isinstance(data, dict):
            raise ValueError("MCP data must be an object")
        else:
            fields = {
                "get_customer_history": ("orders",),
                "get_shipment_summary": ("events", "shipping_limits"),
                "get_payment_timeline": ("payments", "events"),
                "get_refund_timeline": ("events",),
            }.get(tool, ())
            if any(not records(data.get(field)) for field in fields):
                raise ValueError("MCP data contains missing or invalid record lists")
            if tool == "get_policy" and not isinstance(data.get("rules"), dict):
                raise ValueError("MCP policy rules must be an object")

    def _check_scope(self, data: Any, arguments: dict) -> None:
        if isinstance(data, dict):
            for field in ("order_id", "customer_unique_id", "policy_version", "case_id"):
                expected = self.case_id if field == "case_id" else arguments.get(field)
                if expected is not None and field in data and data[field] != expected:
                    raise ValueError(f"MCP response has mismatched {field}")
            for child in data.values():
                if isinstance(child, (dict, list)):
                    self._check_scope(child, arguments)
        elif isinstance(data, list):
            for child in data:
                self._check_scope(child, arguments)

    def send(self, sender: str, payload: dict, recipient: str = "coordinator") -> AgentMessage:
        refs = tuple(sorted(self.consumed.get(sender, set())))
        message = AgentMessage(self.case_id, sender, recipient, deepcopy(payload), refs)
        self.emit(
            "handoff",
            sender,
            target=recipient,
            evidence_refs=list(refs),
            decision_code="RESULT_READY",
        )
        return message

    def receive(self, message: AgentMessage, recipient: str = "coordinator") -> dict:
        if message.case_id != self.case_id or message.recipient != recipient:
            raise ValueError("Invalid A2A message correlation")
        if not set(message.evidence_refs) <= self.evidence.keys():
            raise ValueError("A2A message references unknown evidence")
        return deepcopy(message.payload)


async def entity_agent(ctx: CaseContext) -> AgentMessage:
    case = ctx.case
    candidates = list(dict.fromkeys(case.get("candidate_order_ids", [])))
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    if claimed and claimed not in candidates:
        candidates.append(claimed)
    hint = case.get("customer_unique_id_hint")
    history = (
        await ctx.fetch("entity-agent", "get_customer_history", customer_unique_id=hint)
        if hint
        else None
    )
    history_rows = history.get("orders", []) if isinstance(history, dict) else []
    related = ids(history_rows, "order_id")
    matches = (
        [candidate for candidate in candidates if candidate in related] if candidates else related
    )
    rejected = (
        [candidate for candidate in candidates if candidate not in related] if history else []
    )
    selected = claimed if claimed in matches else matches[0] if len(matches) == 1 else None
    order = None
    if selected:
        order = await ctx.fetch("entity-agent", "get_order", order_id=selected)
    elif not history and len(candidates) == 1:
        order = await ctx.fetch("entity-agent", "get_order", order_id=candidates[0])
        if isinstance(order, dict) and order.get("customer_unique_id"):
            history = await ctx.fetch(
                "entity-agent",
                "get_customer_history",
                customer_unique_id=order["customer_unique_id"],
            )
            history_rows = history.get("orders", []) if isinstance(history, dict) else []
            related = ids(history_rows, "order_id")
            if candidates[0] in related:
                selected = candidates[0]
    resolved = bool(selected and isinstance(order, dict) and order.get("order_id") == selected)
    if resolved:
        rows = [row for row in history_rows if row.get("order_id") == selected]
        if order.get("customer_id") and not any(
            row.get("customer_id") == order["customer_id"] for row in rows
        ):
            resolved = False
    status = "resolved" if resolved else "not_found" if history and not matches else "ambiguous"
    conflicts = []
    if resolved:
        # Discovery describes get_order as authoritative; history is a projection.
        for field in ("order_status", "order_purchase_timestamp", "order_delivered_customer_date"):
            if any(
                row.get(field) != order.get(field)
                for row in history_rows
                if row.get("order_id") == selected
            ):
                conflicts.append(
                    conflict(field, "get_customer_history", "get_order", selected="get_order")
                )
                break
    payload = {
        "resolution": {
            "status": status,
            "resolved_order_ids": [selected] if resolved else [],
            "rejected_candidates": rejected,
            "confidence": 0.97 if resolved else 0.3,
        },
        "customer": {
            "customer_unique_id": history.get("customer_unique_id") if history else None,
            "related_order_ids": related,
        },
        "order": order if resolved else {},
        "conflicts": conflicts,
    }
    return ctx.send("entity-agent", payload)


async def order_agent(ctx: CaseContext, order_id: str) -> AgentMessage:
    items = await ctx.fetch("order-agent", "get_order_items", order_id=order_id)
    products = None
    if ctx.case.get("investigation_scope", {}).get("include_product_context", True):
        products = await ctx.fetch("order-agent", "get_product_context", order_id=order_id)
    return ctx.send("order-agent", analyze_items(items, products))


async def shipment_agent(ctx: CaseContext, order: dict) -> AgentMessage:
    data = await ctx.fetch("shipment-agent", "get_shipment_summary", order_id=order["order_id"])
    return ctx.send("shipment-agent", analyze_shipment(order, data))


async def payment_agent(ctx: CaseContext, order_id: str) -> AgentMessage:
    timeline = await ctx.fetch("payment-agent", "get_payment_timeline", order_id=order_id)
    refund = await ctx.fetch("payment-agent", "get_refund_timeline", order_id=order_id)
    return ctx.send("payment-agent", {"timeline": timeline, "refund": refund})


async def policy_agent(ctx: CaseContext) -> AgentMessage:
    policy = await ctx.fetch(
        "policy-agent", "get_policy", policy_version=ctx.case["policy_version"]
    )
    return ctx.send("policy-agent", {"policy": policy})


def claim_assessments(case: dict, decision: dict, refs: list[str], payment: dict) -> list[dict]:
    result = []
    for claim in case.get("customer_request", {}).get("claims", []):
        verdict = "insufficient_evidence"
        if not decision["blocked"]:
            topic = claim.get("topic")
            if topic == "requested_full_refund":
                captured = payment["analysis"]["captured_total_brl"]
                if decision["amount"] > 0 and captured is not None:
                    verdict = (
                        "supported" if decision["amount"] >= captured else "partially_supported"
                    )
                else:
                    verdict = "unsupported"
            elif topic == decision["issue"]:
                verdict = "supported"
            else:
                verdict = "unsupported"
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": decision["confidence"],
                "evidence_refs": refs,
            }
        )
    return result


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run evidence-backed specialist agents with case-scoped A2A handoffs."""
    ctx = CaseContext(case, gateway, trace, set(await gateway.list_tools()))
    ctx.emit("task_assigned", "coordinator", target="entity-agent", decision_code="RESOLVE_ENTITY")
    entity = ctx.receive(await entity_agent(ctx))
    order = entity["order"]
    ctx.emit("task_assigned", "coordinator", target="policy-agent", decision_code="LOAD_POLICY")
    policy = ctx.receive(await policy_agent(ctx))["policy"]
    if order:
        for actor in ("order-agent", "shipment-agent", "payment-agent"):
            ctx.emit(
                "task_assigned",
                "coordinator",
                target=actor,
                decision_code="INVESTIGATE_RESOLVED_ORDER",
                attributes={"order_id": order["order_id"]},
            )
        messages = await asyncio.gather(
            order_agent(ctx, order["order_id"]),
            shipment_agent(ctx, order),
            payment_agent(ctx, order["order_id"]),
        )
        items, shipment, raw_payment = [ctx.receive(message) for message in messages]
    else:
        items = analyze_items(None, None)
        shipment = analyze_shipment({}, None)
        raw_payment = {"timeline": None, "refund": None}
    payment = analyze_payment(order, raw_payment["timeline"], raw_payment["refund"], items)
    ctx.emit(
        "task_assigned", "coordinator", target="conflict-agent", decision_code="RECONCILE_SOURCES"
    )
    conflicts = [
        *entity["conflicts"],
        *items["conflicts"],
        *shipment["conflicts"],
        *payment["conflicts"],
    ]
    conflicts = list({json.dumps(c, sort_keys=True): c for c in conflicts}.values())
    unresolved = [c for c in conflicts if c["selected_source"] is None]
    ctx.emit(
        "handoff",
        "conflict-agent",
        target="policy-agent",
        decision_code="UNRESOLVED_CONFLICT" if unresolved else "SOURCES_RECONCILED",
        attributes={"conflict_count": len(conflicts)},
    )
    decision = decide(
        order, items, shipment, payment, policy, unresolved, bool(ctx.failures or not order)
    )
    ctx.emit(
        "policy_decided",
        "policy-agent",
        target="coordinator",
        decision_code=decision["issue"].upper(),
        attributes={"refund_brl": decision["amount"], "case_status": decision["status"]},
    )
    refs = sorted(ctx.evidence)
    secondary = []
    if unresolved:
        secondary.append("source_conflict")
    if ctx.failures:
        secondary.append("missing_evidence")
    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": decision["issue"],
            "secondary_issues": secondary,
            "case_status": decision["status"],
            "confidence": decision["confidence"],
        },
        "affected_entities": {
            "order_ids": entity["resolution"]["resolved_order_ids"],
            "item_ids": ids(items["rows"], "order_item_id"),
            "seller_ids": ids(items["rows"], "seller_id"),
            "payment_references": payment["references"],
            "shipment_ids": shipment["shipment_ids"],
        },
        "entity_resolution": entity["resolution"],
        "customer_context": entity["customer"],
        "shipment_analysis": shipment["analysis"],
        "payment_analysis": payment["analysis"],
        "root_cause_analysis": {
            "ranked_causes": [
                {
                    "cause_code": "SOURCE_CONFLICT" if unresolved else decision["issue"].upper(),
                    "rank": 1,
                }
            ],
            "responsible_parties": decision["parties"],
        },
        "evidence_refs": refs,
        "claim_assessments": claim_assessments(case, decision, refs, payment),
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": decision["amount"],
            "refund_lines": [
                {
                    "reason_code": decision["issue"].upper(),
                    "amount_brl": decision["amount"],
                    "entity_id": order["order_id"],
                }
            ]
            if decision["amount"]
            else [],
        },
        "resolution_actions": [decision["action"]],
    }
    ctx.emit("handoff", "coordinator", target="verifier", decision_code="VERIFY_OUTPUT")
    ctx.emit("task_assigned", "coordinator", target="verifier", decision_code="CHECK_INVARIANTS")
    trace.contracts.validate_output(output, "solver output")
    verify_output(output, case, set(ctx.evidence))
    ctx.emit(
        "verification_completed",
        "verifier",
        target="coordinator",
        decision_code="VERIFIED_WITH_GAPS" if decision["blocked"] else "VERIFIED",
        attributes={
            "schema_valid": True,
            "invariants_valid": True,
            "mcp_calls": ctx.calls,
            "failed_tools": len(ctx.failures),
            "unresolved_conflicts": len(unresolved),
        },
    )
    return output
