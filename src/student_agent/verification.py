"""Independent cross-field checks, shared by the solver and artifact validator."""

from __future__ import annotations

from .analysis import money


def verify_output(output: dict, case: dict, evidence_refs: set[str]) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(f"{case['case_id']}: {message}")

    require(output["case_id"] == case["case_id"], "case_id mismatch")
    refs = set(output["evidence_refs"])
    require(refs <= evidence_refs, "output contains unconsumed or cross-case evidence")
    entity = output["entity_resolution"]
    resolved = set(entity["resolved_order_ids"])
    require(resolved == set(output["affected_entities"]["order_ids"]), "order scope mismatch")
    require(not resolved.intersection(entity["rejected_candidates"]), "resolved candidate rejected")
    require((entity["status"] == "resolved") == bool(resolved), "entity status mismatch")
    for claim in output.get("claim_assessments", []):
        require(set(claim["evidence_refs"]) <= refs, "claim evidence not linked to output")
        if claim["verdict"] != "insufficient_evidence":
            require(bool(claim["evidence_refs"]), "claim verdict has no evidence")
    expected = {c["claim_id"] for c in case.get("customer_request", {}).get("claims", [])}
    actual = [c["claim_id"] for c in output.get("claim_assessments", [])]
    require(set(actual) == expected and len(actual) == len(expected), "claim coverage mismatch")
    financial, payment = output["financial_resolution"], output["payment_analysis"]
    amount = money(financial["recommended_refund_brl"])
    require(
        sum((money(line["amount_brl"]) for line in financial["refund_lines"]), money(0)) == amount,
        "refund lines do not reconcile",
    )
    captured, refunded, balance = [
        money(payment[f"{field}_total_brl"]) for field in ("captured", "refunded", "refundable")
    ]
    if captured is not None and refunded is not None:
        require(refunded <= captured, "refunded exceeds captured")
        if balance is not None:
            require(balance == captured - refunded, "refundable balance mismatch")
    status = output["assessment"]["case_status"]
    if amount:
        require(status == "action_required", "refund without action_required")
        require(balance is not None and amount <= balance, "refund exceeds known available balance")
        require(bool(resolved), "refund without resolved entity")
    if status == "no_action":
        require(amount == 0, "no_action case requests money")
    if entity["status"] != "resolved":
        require(status == "needs_investigation", "unresolved entity requires investigation")
    sellers = set(output["affected_entities"]["seller_ids"])
    late = set(output["shipment_analysis"]["late_seller_ids"])
    require(late <= sellers, "late seller outside affected entities")
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller":
            require(party["party_id"] in sellers, "responsible seller outside order")
    for item in output["data_conflicts"]:
        require(
            item["selected_source"] is None or item["selected_source"] in item["sources"],
            "selected source is absent from conflict",
        )
        if item["selected_source"] is None:
            require(status == "needs_investigation", "unresolved source conflict finalized")
