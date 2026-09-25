# L3B LLM Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the existing `solve_case()` contract so all 100 supplied L3B cases produce schema-valid outputs and audited multi-agent traces using Cloudflare Workers AI.

**Architecture:** `workers_ai.py` owns the one-request-per-case Cloudflare boundary and strict JSON parsing. `workflow.py` owns case-scoped MCP evidence collection, logical agent trace events, prompting, deterministic evidence/output checks, and final contract-shaped output. Tests use in-memory fakes and never create or modify competition inputs.

**Tech Stack:** Python 3.11+, asyncio, httpx2, jsonschema, pytest, Cloudflare Workers AI, MCP.

---

## File Map

- Create `src/student_agent/workers_ai.py`: environment loading, Cloudflare request, JSON response parsing.
- Modify `src/student_agent/workflow.py`: MCP orchestration, trace lifecycle, model prompt, verification.
- Create `tests/test_workflow.py`: focused client and workflow behavior tests.
- Modify `ARCHITECTURE.md`: replace starter placeholders with implemented decisions.

No file under `inputs/` is created or modified.

### Task 1: Workers AI Boundary

**Files:**
- Create: `src/student_agent/workers_ai.py`
- Create: `tests/test_workflow.py`

- [ ] **Step 1: Write failing tests for environment validation and response parsing**

```python
def test_workers_ai_settings_require_all_values(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID"):
        WorkersAISettings.load()


def test_parse_model_object_accepts_plain_json():
    assert parse_model_object('{"case_id":"CASE_001"}') == {"case_id": "CASE_001"}


def test_parse_model_object_accepts_fenced_json():
    text = '```json\n{"case_id":"CASE_001"}\n```'
    assert parse_model_object(text) == {"case_id": "CASE_001"}
```

- [ ] **Step 2: Run the tests and confirm the missing module failure**

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_workflow.py -v
```

Expected: FAIL because `student_agent.workers_ai` does not exist.

- [ ] **Step 3: Implement settings, strict parsing, and the Cloudflare call**

```python
@dataclass(frozen=True)
class WorkersAISettings:
    account_id: str
    api_token: str
    model: str

    @classmethod
    def load(cls) -> "WorkersAISettings":
        values = {
            "CLOUDFLARE_ACCOUNT_ID": os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(),
            "CLOUDFLARE_API_TOKEN": os.getenv("CLOUDFLARE_API_TOKEN", "").strip(),
            "CLOUDFLARE_AI_MODEL": os.getenv("CLOUDFLARE_AI_MODEL", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"missing Workers AI settings: {', '.join(missing)}")
        return cls(values["CLOUDFLARE_ACCOUNT_ID"], values["CLOUDFLARE_API_TOKEN"], values["CLOUDFLARE_AI_MODEL"])


def parse_model_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("Workers AI response must be a JSON object")
    return value


async def request_object(prompt: str) -> dict[str, Any]:
    settings = WorkersAISettings.load()
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.account_id}/ai/run/{settings.model}"
    )
    async with httpx2.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.api_token}"},
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 4096,
            },
        )
        response.raise_for_status()
        payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Workers AI failed: {payload.get('errors', [])}")
    return parse_model_object(payload["result"]["response"])
```

- [ ] **Step 4: Run the focused tests**

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_workflow.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/student_agent/workers_ai.py tests/test_workflow.py
git commit -m "feat: add Workers AI client"
```

### Task 2: Case-Scoped Evidence Collection

**Files:**
- Modify: `src/student_agent/workflow.py`
- Modify: `tests/test_workflow.py`

- [ ] **Step 1: Write a failing test for scoped, cached MCP calls**

```python
def test_collect_evidence_is_case_scoped_and_cached():
    gateway = FakeGateway()
    trace = FakeTrace()
    case = sample_case()

    bundle = asyncio.run(collect_evidence(case, gateway, trace))

    assert bundle["case_id"] == case["case_id"]
    assert all(call["case_id"] == case["case_id"] for call in gateway.calls)
    assert len(gateway.calls) == len({(call["tool"], tuple(sorted(call["args"].items()))) for call in gateway.calls})
    assert set(bundle["evidence_refs"]) == {
        event["evidence_refs"][0]
        for event in trace.events
        if event["event_type"] == "tool_result_consumed"
    }
```

- [ ] **Step 2: Run the test and confirm `collect_evidence` is missing**

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_workflow.py::test_collect_evidence_is_case_scoped_and_cached -v
```

Expected: FAIL because `collect_evidence` is not defined.

- [ ] **Step 3: Implement one case-local cached caller**

```python
async def collect_evidence(case, gateway, trace):
    case_id = case["case_id"]
    cache = {}
    evidence = []

    async def call(actor, tool_name, **arguments):
        key = (tool_name, tuple(sorted(arguments.items())))
        if key not in cache:
            cache[key] = await gateway.call(tool_name, case_id=case_id, **arguments)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[cache[key]["evidence_ref"]],
            )
        return cache[key]

    # Query both supplied candidates for entity resolution, then use the matching
    # order for customer, product, shipment, payment/refund, and policy evidence.
```

The implemented collector calls:

- `get_order` for each unique supplied candidate;
- `get_customer_history` for the supplied customer hint;
- `get_order_items`, `get_product_context`, and `get_sellers`;
- `get_shipment_summary`;
- `get_order_payments`, `get_payment_timeline`, and `get_refund_timeline`;
- `get_policy`.

Calls that return an explicit not-found payload remain available to the entity
agent but are not used as the resolved order for downstream calls.

- [ ] **Step 4: Run the focused test**

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_workflow.py::test_collect_evidence_is_case_scoped_and_cached -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/student_agent/workflow.py tests/test_workflow.py
git commit -m "feat: collect case-scoped MCP evidence"
```

### Task 3: LLM-Orchestrated Output And Trace

**Files:**
- Modify: `src/student_agent/workflow.py`
- Modify: `tests/test_workflow.py`

- [ ] **Step 1: Write a failing end-to-end unit test**

```python
def test_solve_case_returns_valid_l3b_output(monkeypatch, tmp_path):
    expected = sample_valid_output()

    async def fake_request_object(prompt):
        assert "entity-agent" in prompt
        assert "shipment-agent" in prompt
        assert "payment-refund-agent" in prompt
        assert "policy-conflict-agent" in prompt
        assert "verifier" in prompt
        return expected

    monkeypatch.setattr("student_agent.workflow.request_object", fake_request_object)
    gateway = FakeGateway()
    contracts = Contracts(PROJECT_ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    result = asyncio.run(solve_case(sample_case(), gateway, trace))

    contracts.validate_output(result, "test output")
    assert result["case_id"] == "L3B_CASE_001"
```

- [ ] **Step 2: Run the test and confirm the placeholder failure**

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_workflow.py::test_solve_case_returns_valid_l3b_output -v
```

Expected: FAIL because `solve_case()` has no implementation.

- [ ] **Step 3: Implement the coordinator prompt and trace lifecycle**

`solve_case()` will:

1. emit `task_assigned` for entity/customer, order/product, shipment,
   payment/refund, and policy/conflict roles;
2. gather evidence once through `collect_evidence`;
3. emit specialist `handoff` events to the coordinator;
4. serialize the existing case and MCP evidence into one compact prompt;
5. call `request_object(prompt)`;
6. enforce `schema_version` and `case_id`;
7. reject evidence refs not present in the current case bundle;
8. emit `policy_decided`, a verifier handoff, and `verification_completed`;
9. return the object for the CLI's public-schema validation.

The system prompt includes all enum choices from the public schema and directs
the model to return only the required object, use BRL numbers, preserve claim
IDs, and avoid unsupported facts.

- [ ] **Step 4: Add one repair attempt for malformed model output**

```python
try:
    output = await request_object(prompt)
    verify_output(output, case, allowed_refs)
except (json.JSONDecodeError, KeyError, TypeError, ValueError) as first_error:
    output = await request_object(build_repair_prompt(prompt, str(first_error)))
    verify_output(output, case, allowed_refs)
```

Do not retry authentication, quota, or transport failures. If both model
objects fail local/schema validation, return a conservative
`insufficient_evidence` output using only current-case MCP references.

- [ ] **Step 5: Run workflow tests and the full unit suite**

Run:

```powershell
.\.venv\Scripts\pytest.exe tests/test_workflow.py -v
.\.venv\Scripts\pytest.exe tests/test_starter.py tests/test_workflow.py -q
```

Expected: all starter and workflow tests pass. `test_release_safety.py` is not
run because its purpose is to verify a distributable starter repository has no
competition payload, while this workspace intentionally contains the user's
official input bundle.

- [ ] **Step 6: Commit**

```powershell
git add src/student_agent/workflow.py tests/test_workflow.py
git commit -m "feat: orchestrate L3B investigation"
```

### Task 4: Architecture Record

**Files:**
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Complete the architecture record**

Document the implemented role ownership, one-request-per-case model flow,
case-local MCP cache, one transient MCP retry ceiling, evidence provenance,
conflict handling, verifier invariants, and Cloudflare model configuration.
State explicitly that no input is generated or modified.

- [ ] **Step 2: Scan for unfinished sections**

Run:

```powershell
rg -n "TODO|TBD" ARCHITECTURE.md
```

Expected: no matches.

- [ ] **Step 3: Commit**

```powershell
git add ARCHITECTURE.md
git commit -m "docs: describe L3B agent workflow"
```

### Task 5: Live Verification

**Files:**
- Generated and ignored: `outputs/*.json`
- Generated and ignored: `traces/trace.jsonl`

- [ ] **Step 1: Verify local configuration without printing secrets**

Run a redacted environment check requiring:

```text
COMPETITION_TEAM_API_KEY
MCP_ENDPOINT
CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID
CLOUDFLARE_AI_MODEL
```

Expected: every variable is present.

- [ ] **Step 2: Run one real case through `solve_case()`**

Use `load_case_set()`, the existing MCP connection, and the first existing case.
Do not create a separate input or change `case-set.json`.

Expected: a schema-valid result for `L3B_CASE_001`, Workers AI success, and
case-scoped trace events.

- [ ] **Step 3: Run all 100 existing cases**

Run:

```powershell
.\.venv\Scripts\day09.exe run
```

Expected: exit code 0 and 100 JSON files under `outputs/`.

- [ ] **Step 4: Validate artifacts**

Run:

```powershell
.\.venv\Scripts\day09.exe validate
```

Expected:

```text
OK: 100 outputs / <positive count> trace events
```

- [ ] **Step 5: Run static checks**

Run:

```powershell
.\.venv\Scripts\ruff.exe check src tests
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 6: Review generated results without committing them**

Confirm `git status --short` contains no input, output, trace, `.env`, or secret
changes. Generated competition artifacts remain ignored.
