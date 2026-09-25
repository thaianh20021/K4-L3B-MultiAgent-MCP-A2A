# L3B LLM-Orchestrated Workflow Design

## Goal

Implement `solve_case()` so the existing 100 L3B inputs run end to end with
Cloudflare Workers AI model `@cf/meta/llama-3.1-8b-instruct-fp8-fast`, audited
MCP evidence, schema-valid outputs, and observable multi-agent traces.

The workflow must not create or modify any input files.

## Architecture

Each case runs through a coordinator and five logical specialist roles:

1. Entity agent resolves the order from the claimed ID and candidates.
2. Customer/order agent gathers customer, order, item, seller, and product evidence.
3. Shipment agent evaluates delivery state and responsibility.
4. Payment/refund agent reconciles captures, refunds, and refundable amount.
5. Policy/conflict agent applies policy and records source conflicts.
6. Verifier checks evidence ownership, cross-field consistency, and output schema.

The coordinator discovers MCP tools once through the existing gateway, assigns
role-specific work, records handoffs, and sends the compact case plus gathered
evidence to Workers AI. The model returns the final contract-shaped assessment.
Python validates and normalizes the response before finalization.

## Evidence Flow

- Every MCP call includes the current `case_id`.
- Candidate resolution starts with `get_order` and rejects candidates that are
  missing or inconsistent with the customer hint.
- Calls are cached within one case and never reused across cases.
- Only evidence returned by MCP is submitted in `evidence_refs`.
- Every consumed evidence reference emits `tool_result_consumed`.
- Tool calls are selected by case scope and claim topics; retries are limited to
  one transient retry.

## Model Flow

Workers AI receives one coordinator request per case containing the case,
role-labelled evidence, allowed enum values, and the required output shape.
The prompt instructs the model to use only supplied evidence and return a single
JSON object. No prompts or hidden reasoning are written to trace.

If the model response is malformed, Python performs one repair request. If the
response remains invalid, the run fails visibly rather than inventing evidence.

## Trace

Each case emits:

- `case_received` from the CLI;
- `task_assigned` for each active specialist;
- `tool_result_consumed` for each evidence object used;
- `handoff` between coordinator, specialists, conflict resolver, and verifier;
- `policy_decided` when policy evidence is applied;
- `verification_completed` after deterministic checks;
- `case_finalized` from the CLI.

## Validation And Tests

Tests use fake gateway, trace, and Workers AI responses. They verify:

- no input files are written;
- MCP calls remain case-scoped and cached;
- evidence references and trace events match consumed evidence;
- malformed model output gets one repair attempt;
- a representative result validates against the public L3B schema.

End-to-end verification runs `pytest`, one-case smoke execution, the full
`day09 run`, and `day09 validate`.

## Operational Limits

The implementation uses the existing dependencies and standard library. It does
not add input data, a framework, or additional configuration files. Full
LLM-driven execution may consume substantial Workers AI quota; failures remain
restartable by rerunning the complete command, while MCP caching is intentionally
limited to each case for audit correctness.
