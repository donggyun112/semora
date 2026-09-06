# Pydantic Durability Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist Pydantic AI's native deferred request value, separate Semora's policy and effect-journal responsibilities behind the existing outermost capability, and prove that Semora composes with Pydantic durability capabilities without weakening fail-closed recovery.

**Architecture:** `AgentRuntime` continues to own leases, suspension, resume, recovery, and transcript lifecycle. `ExecutionBoundary` remains one outermost Pydantic capability, but delegates durable model/tool transitions to `EffectJournal` and tool-policy decisions to `PolicyRunner`. Resume reconstructs `DeferredToolRequests` from persisted data and lets Pydantic create `DeferredToolResults` with `build_results()`.

**Tech Stack:** Python 3.10+, Pydantic 2, Pydantic AI 2.38–2.x, pydantic-ai-harness 0.29, pytest/pytest-asyncio, Ruff, mypy, uv.

**Spec:** docs/superpowers/specs/2026-09-07-pydantic-durability-integration-design.md

## Global Constraints

- Pydantic AI owns the agent loop, models, messages, tool definitions, tool calls, deferred result construction, and capability protocol.
- Semora keeps one execution path and one outermost `ExecutionBoundary`; do not introduce another agent class, decorator, or graph loop.
- A tool result must be committed before `post_tool_use` is journaled. An unresolved started effect remains `Indeterminate` unless the caller opts into retry.
- Approval remains an input to `on_resume`; it is not unconditional authorization.
- New continuations use native `DeferredToolRequests`; the old `continuation["calls"]` shape remains readable through the 0.5.x line.
- `semora-store` remains dependency-free and must not import Pydantic.
- Keep `pydantic-ai-slim>=2.38,<3` unless a production import fails under 2.38. Version-gate development-only composition tests if necessary.
- Do not implement a production `BaseDurabilityCapability` until Pydantic exposes stable serializable invocation identity or Semora has a transactional operation cursor.
- Use tests to preserve behavior before moving code. Commit after each task passes its focused checks.

---

### Task 1: Replace the hand-built parked-call payload with native deferred requests

**Files:**

- Modify: `tests/test_hitl.py`
- Modify: `packages/semora/src/semora/runtime.py`

- [ ] **Step 1: Add failing tests for the new persisted shape and argument overrides**

Add imports and tests that inspect the active suspension after a real park. The test must validate stored data through Pydantic rather than assert an implementation-only dictionary layout:

```python
from pydantic import TypeAdapter
from pydantic_ai import DeferredToolRequests
from semora.runtime import ACTIVE_SUSPENSION


DEFERRED_REQUESTS = TypeAdapter(DeferredToolRequests)


async def test_a_park_persists_the_native_deferred_request() -> None:
    store, files = MemorySteps(), Files()
    agent, _ = scripted(["stale.md"])
    agent.tool_plain(files.write)

    with pytest.raises(AgentSuspended):
        await AgentRuntime(store).run(
            "native-park", agent, "delete", controls=ControlPlane(pre_tool_use=ask_for("stale.md"))
        )

    active = await store.read("native-park", ACTIVE_SUSPENSION)
    continuation = active.value["continuation"]
    requests = DEFERRED_REQUESTS.validate_python(continuation["deferred"])
    assert [call.tool_call_id for call in requests.approvals] == ["c00"]
    assert requests.metadata["c00"]["pending_id"] == "approve-c00"
    assert "calls" not in continuation


async def test_resume_lets_pydantic_apply_approved_argument_overrides() -> None:
    store, files = MemorySteps(), Files()
    agent, _ = scripted(["stale.md"])
    agent.tool_plain(files.write)
    controls = ControlPlane(pre_tool_use=ask_for("stale.md"))

    with pytest.raises(AgentSuspended) as parked:
        await AgentRuntime(store).run("override", agent, "delete", controls=controls)

    await AgentRuntime(store).resume(
        "override",
        parked.value.pending_id or "",
        {"type": "approve", "args": {"path": "reviewed.md"}},
        agent,
        controls=controls,
    )
    assert files.ran == ["reviewed.md"]
```

- [ ] **Step 2: Run the two tests and verify the storage-shape test fails**

Run:

```bash
uv run --offline pytest -q \
  tests/test_hitl.py::test_a_park_persists_the_native_deferred_request \
  tests/test_hitl.py::test_resume_lets_pydantic_apply_approved_argument_overrides
```

Expected: the first test fails because the continuation has `calls` and lacks `deferred`; the existing manual result path may make the override test pass.

- [ ] **Step 3: Persist and decode `DeferredToolRequests` with a `TypeAdapter`**

In `runtime.py`, define one adapter next to `_PART`:

```python
_DEFERRED_REQUESTS = TypeAdapter(DeferredToolRequests)
```

Change `_park()` so the continuation stores the full Pydantic value:

```python
"deferred": _DEFERRED_REQUESTS.dump_python(requests, mode="json"),
```

Replace `_decode_parked()` with a native reader plus the 0.5.x compatibility branch:

```python
def _deferred_requests(active: dict[str, Any]) -> DeferredToolRequests:
    continuation = active["continuation"]
    encoded = continuation.get("deferred")
    if encoded is not None:
        requests = _DEFERRED_REQUESTS.validate_python(encoded)
    else:
        entries = continuation["calls"]
        approvals = [
            ToolCallPart(
                tool_name=entry["call"]["tool_name"],
                args=entry["call"]["args"],
                tool_call_id=entry["call"]["tool_call_id"],
            )
            for entry in entries
        ]
        requests = DeferredToolRequests(
            approvals=approvals,
            metadata={
                call.tool_call_id: dict(entry["request"])
                for call, entry in zip(approvals, entries, strict=True)
            },
        )

    actual = [call.tool_call_id for call in requests.approvals]
    expected = list(active["call_ids"])
    if actual != expected or any(call_id not in requests.metadata for call_id in actual):
        raise ValueError("stored deferred requests do not match the active suspension")
    return requests


def _decode_parked(active: dict[str, Any]) -> list[tuple[ToolCallPart, dict[str, Any]]]:
    requests = _deferred_requests(active)
    return [(call, dict(requests.metadata[call.tool_call_id])) for call in requests.approvals]
```

Keep failures fail-closed: do not default missing `calls`, missing metadata, invalid tool-call IDs, or malformed Pydantic values to an empty batch.

- [ ] **Step 4: Give Pydantic ownership of deferred result construction**

In `_finalize()`, reload the same persisted request and replace direct `DeferredToolResults`
construction:

```python
requests = _deferred_requests(active)
deferred_tool_results = requests.build_results(approvals=approvals)

outcome = await self._attempt(
    rejoined,
    token,
    agent,
    message_history=history,
    deferred_tool_results=deferred_tool_results,
    # existing arguments unchanged
)
```

`approvals` still maps host answers to `ToolApproved` or `ToolDenied`; Pydantic now validates the mapping and applies override arguments.

- [ ] **Step 5: Run the focused tests and the full HITL module**

Run:

```bash
uv run --offline pytest -q tests/test_hitl.py
```

Expected: all HITL tests pass.

- [ ] **Step 6: Commit the native continuation change**

```bash
git add packages/semora/src/semora/runtime.py tests/test_hitl.py
git commit -m "refactor: persist native deferred requests"
```

---

### Task 2: Preserve 0.5.x continuations and reject mismatched durable state

**Files:**

- Modify: `tests/test_hitl.py`
- Modify: `packages/semora/src/semora/runtime.py`

- [ ] **Step 1: Add a helper that rewrites a real native park into the legacy shape**

The helper must acquire a fencing token instead of mutating `MemorySteps` internals:

```python
async def rewrite_as_legacy_continuation(store: MemorySteps, branch_id: str) -> None:
    record = await store.read(branch_id, ACTIVE_SUSPENSION)
    active = dict(record.value)
    continuation = dict(active["continuation"])
    requests = DEFERRED_REQUESTS.validate_python(continuation.pop("deferred"))
    continuation["calls"] = [
        {
            "call": {
                "tool_name": call.tool_name,
                "args": call.args_as_dict(),
                "tool_call_id": call.tool_call_id,
            },
            "request": requests.metadata[call.tool_call_id],
        }
        for call in requests.approvals
    ]
    token = await store.acquire(branch_id, "legacy-fixture", 30)
    assert token
    try:
        await store.write_control(
            branch_id,
            ACTIVE_SUSPENSION,
            {**active, "continuation": continuation},
            token,
        )
    finally:
        await store.release(branch_id, "legacy-fixture")
```

- [ ] **Step 2: Add a passing migration test and failing corruption tests**

Add these behaviors:

```python
async def test_resume_reads_a_legacy_0_5_continuation() -> None:
    store, files = MemorySteps(), Files()
    agent, _ = scripted(["stale.md"])
    agent.tool_plain(files.write)
    controls = ControlPlane(pre_tool_use=ask_for("stale.md"))
    with pytest.raises(AgentSuspended) as parked:
        await AgentRuntime(store).run("legacy", agent, "delete", controls=controls)

    await rewrite_as_legacy_continuation(store, "legacy")
    outcome = await AgentRuntime(store).resume(
        "legacy", parked.value.pending_id or "", {"type": "approve"}, agent, controls=controls
    )

    assert outcome.output == "done"
    assert files.ran == ["stale.md"]


async def test_resume_rejects_mismatched_deferred_call_ids() -> None:
    store, files = MemorySteps(), Files()
    agent, _ = scripted(["stale.md"])
    agent.tool_plain(files.write)
    controls = ControlPlane(pre_tool_use=ask_for("stale.md"))
    with pytest.raises(AgentSuspended) as parked:
        await AgentRuntime(store).run("mismatch", agent, "delete", controls=controls)
    record = await store.read("mismatch", ACTIVE_SUSPENSION)
    active = {**record.value, "call_ids": ["different"]}
    await replace_active_suspension(store, "mismatch", active)

    with pytest.raises(ValueError, match="do not match"):
        await AgentRuntime(store).resume(
            "mismatch",
            parked.value.pending_id or "",
            {"type": "approve"},
            agent,
            controls=controls,
        )
    assert files.ran == []


async def test_resume_rejects_a_malformed_native_deferred_value() -> None:
    store, files = MemorySteps(), Files()
    agent, _ = scripted(["stale.md"])
    agent.tool_plain(files.write)
    controls = ControlPlane(pre_tool_use=ask_for("stale.md"))
    with pytest.raises(AgentSuspended) as parked:
        await AgentRuntime(store).run("malformed", agent, "delete", controls=controls)
    record = await store.read("malformed", ACTIVE_SUSPENSION)
    active = dict(record.value)
    active["continuation"] = {
        **active["continuation"],
        "deferred": {"bad": "shape"},
    }
    await replace_active_suspension(store, "malformed", active)

    with pytest.raises(ValidationError):
        await AgentRuntime(store).resume(
            "malformed",
            parked.value.pending_id or "",
            {"type": "approve"},
            agent,
            controls=controls,
        )
    assert files.ran == []
```

Define `replace_active_suspension()` beside the legacy helper. It acquires owner
`"continuation-fixture"`, writes `ACTIVE_SUSPENSION`, and releases that owner in a `finally`
block. Import `ValidationError` from `pydantic`. Do not mock `_deferred_requests()`.

- [ ] **Step 3: Run the new tests and tighten validation until corruption fails before `_attempt()`**

Run:

```bash
uv run --offline pytest -q tests/test_hitl.py -k "legacy or corrupt"
```

Expected: the legacy continuation resumes successfully; both corrupted shapes raise and `files.ran` remains empty.

- [ ] **Step 4: Run HITL and recovery regression tests**

Run:

```bash
uv run --offline pytest -q tests/test_hitl.py tests/test_recovery.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit compatibility and fail-closed validation**

```bash
git add packages/semora/src/semora/runtime.py tests/test_hitl.py
git commit -m "test: preserve deferred continuation compatibility"
```

---

### Task 3: Extract durable effect transitions into `EffectJournal`

**Files:**

- Create: `packages/semora/src/semora/journal.py`
- Modify: `packages/semora/src/semora/effects.py`
- Modify: `packages/semora/src/semora/runtime.py`
- Modify: `tests/test_effect_boundary.py`
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: Add collaborator-level characterization tests**

Add tests for the four behaviors the extraction must preserve:

```python
async def test_effect_journal_replays_a_committed_tool_without_running_the_body() -> None:
    store = MemorySteps()
    token = await store.acquire("journal-replay", "test", 30)
    assert token
    journal = EffectJournal(store, "journal-replay", token, retry_running=False)
    calls = 0

    async def body(args: object) -> str:
        nonlocal calls
        calls += 1
        return f"value:{args}"

    try:
        first = await journal.tool("c1", "one", body)
        second = await journal.tool("c1", "two", body)
    finally:
        await store.release("journal-replay", "test")

    assert first == second == {"ok": True, "value": "value:one"}
    assert calls == 1


async def test_effect_journal_refuses_an_unresolved_started_tool() -> None:
    store = MemorySteps()
    token = await store.acquire("journal-running", "test", 30)
    assert token
    assert await store.start("journal-running", step_key("c1"), token)
    journal = EffectJournal(store, "journal-running", token, retry_running=False)

    async def body(args: object) -> object:
        return args

    try:
        with pytest.raises(Indeterminate):
            await journal.tool("c1", "value", body)
    finally:
        await store.release("journal-running", "test")


async def test_effect_journal_commits_a_failure_as_a_result() -> None:
    store = MemorySteps()
    token = await store.acquire("journal-failure", "test", 30)
    assert token
    journal = EffectJournal(store, "journal-failure", token, retry_running=False)

    async def body(args: object) -> object:
        raise ValueError(f"bad {args}")

    try:
        record = await journal.tool("c1", "input", body)
    finally:
        await store.release("journal-failure", "test")

    assert record == {"ok": False, "error": "bad input"}
    assert (await store.read("journal-failure", step_key("c1"))).status == "done"


async def test_effect_journal_replays_one_post_tool_projection() -> None:
    store = MemorySteps()
    token = await store.acquire("journal-project", "test", 30)
    assert token
    journal = EffectJournal(store, "journal-project", token, retry_running=False)
    projected: list[dict[str, object]] = []

    async def project(record: dict[str, object]) -> None:
        projected.append(dict(record))

    committed = {"ok": True, "value": "done"}
    try:
        first = await journal.project_once("c1", committed, project)
        second = await journal.project_once("c1", committed, project)
    finally:
        await store.release("journal-project", "test")

    assert first == second == committed
    assert projected == [committed]
```

Import `EffectJournal` from `semora.journal`. These tests intentionally target the collaborator because the existing recovery tests continue to cover the assembled capability.

- [ ] **Step 2: Run the new tests and verify import failure**

Run:

```bash
uv run --offline pytest -q tests/test_effect_boundary.py -k effect_journal
```

Expected: collection fails because `semora.journal` does not exist.

- [ ] **Step 3: Move keying and effect state transitions into `journal.py`**

Create `EffectJournal(store, branch_id, token, *, retry_running)` with four typed async methods:
`recorded(call_id) -> bool`, `model(request, handler) -> ModelResponse`,
`tool(call_id, args, handler) -> dict[str, Any]`, and
`project_once(call_id, record, project) -> dict[str, Any]`. Use the precise callable types shown
by the tests and the existing Pydantic hook signatures. The class contains no controls, run-node,
or transcript-lifecycle logic.

Move `step_key`, `after_key`, `model_step_key`, `_NOTHING_HAPPENED`, `_RUNTIME_SIGNALS`, and the model-response adapter with these methods. `tool()` must keep the current order:

```python
await store.start(self.branch_id, key, self.token)
record = await handler(args)  # or committed ordinary-exception result
await store.finish_effect(self.branch_id, key, record, self.token)
return record
```

`project_once()` must look up the `after:` marker before invoking `project`, and only write that marker after `project` returns.

- [ ] **Step 4: Delegate the Pydantic hooks without changing their semantics**

Construct `self.journal` in `ExecutionBoundary.__init__()`. Reduce the hooks to coordination:

```python
async def wrap_model_request(
    self,
    ctx: RunContext[Any],
    *,
    request_context: ModelRequestContext,
    handler: Callable[[ModelRequestContext], Awaitable[ModelResponse]],
) -> ModelResponse:
    return await self.journal.model(request_context, handler)


async def wrap_tool_execute(
    self,
    ctx: RunContext[Any],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: Any,
    handler: Callable[[Any], Awaitable[Any]],
) -> Any:
    record = await self.journal.tool(call.tool_call_id, args, handler)

    async def project(committed: dict[str, Any]) -> None:
        if self.controls is None:
            return
        result = (
            committed["value"]
            if committed["ok"]
            else {"type": "error", "message": committed["error"]}
        )
        await self.controls.post_tool_use(self._ctx(ctx, tool=tool_def), call, result)

    record = await self.journal.project_once(call.tool_call_id, record, project)
    if record["ok"]:
        return record["value"]
    raise ToolFailed(record["error"])
```

Change the gate's replay check to `await self.journal.recorded(call.tool_call_id)`. Import `step_key` from `journal.py` in `runtime.py`. Re-export the three key functions from `effects.py` for source compatibility during 0.5.x.

- [ ] **Step 5: Update the layer test**

Place `journal` below `effects` and above its dependencies:

```python
LAYERS = [
    "contracts",
    "ids",
    "controls",
    "transcript",
    "journal",
    "effects",
    "dispatch",
    "runtime",
]
```

- [ ] **Step 6: Run focused and assembled boundary tests**

Run:

```bash
uv run --offline pytest -q \
  tests/test_effect_boundary.py \
  tests/test_recovery.py \
  tests/test_packaging.py
uv run --offline mypy
```

Expected: all tests and mypy pass; no result-first or `Indeterminate` behavior changes.

- [ ] **Step 7: Commit the effect journal extraction**

```bash
git add \
  packages/semora/src/semora/journal.py \
  packages/semora/src/semora/effects.py \
  packages/semora/src/semora/runtime.py \
  tests/test_effect_boundary.py \
  tests/test_packaging.py
git commit -m "refactor: isolate the durable effect journal"
```

---

### Task 4: Extract tool policy decisions into `PolicyRunner`

**Files:**

- Create: `packages/semora/src/semora/policy.py`
- Modify: `packages/semora/src/semora/effects.py`
- Modify: `tests/test_effect_boundary.py`
- Modify: `tests/test_hitl.py`
- Modify: `tests/test_packaging.py`

- [ ] **Step 1: Add focused policy-runner tests before moving behavior**

Cover fresh, resumed, replayed, and batched calls:

```python
async def test_policy_runner_asks_pre_tool_for_a_fresh_call() -> None:
    async def ask(ctx: Ctx, call: ToolCallPart) -> ToolDecision:
        return Suspend({"pending_id": f"approve-{call.tool_call_id}"})

    runner = PolicyRunner(
        controls=ControlPlane(pre_tool_use=Permissions(ask)),
        rules_version="v1",
        subject="민수",
        resumed={},
        regate=(),
    )
    call = ToolCallPart("write", {"path": "a.txt"}, tool_call_id="c1")
    decision = await runner.decide_tool(
        Ctx(turn=0), call, call.args_as_dict(), approved=False, recorded=False
    )
    assert decision == Suspend({"pending_id": "approve-c1"})


async def test_policy_runner_revalidates_approved_arguments_on_resume() -> None:
    seen: list[str] = []

    async def recheck(ctx: Ctx, call: ToolCallPart, resume: ResumeInput) -> ToolDecision:
        seen.append(str(call.args_as_dict()["path"]))
        return Continue()

    runner = PolicyRunner(
        controls=ControlPlane(on_resume=recheck),
        rules_version="v2",
        subject="민수",
        resumed={"c1": Resumed({"type": "approve"}, {"pending_id": "p1"}, "v1")},
        regate=(),
    )
    call = ToolCallPart("write", {"path": "old.txt"}, tool_call_id="c1")
    decision = await runner.decide_tool(
        Ctx(turn=1),
        call,
        {"path": "reviewed.txt"},
        approved=True,
        recorded=False,
    )
    assert isinstance(decision, Continue)
    assert seen == ["reviewed.txt"]


async def test_policy_runner_does_not_regate_a_committed_call() -> None:
    async def unexpected(ctx: Ctx, call: ToolCallPart) -> ToolDecision:
        raise AssertionError("a committed effect must replay without a fresh gate")

    runner = PolicyRunner(
        controls=ControlPlane(pre_tool_use=Permissions(unexpected)),
        rules_version="v1",
        subject="",
        resumed={},
        regate=(),
    )
    call = ToolCallPart("write", {"path": "a.txt"}, tool_call_id="c1")
    decision = await runner.decide_tool(
        Ctx(turn=0), call, call.args_as_dict(), approved=False, recorded=True
    )
    assert isinstance(decision, Continue)


async def test_policy_runner_keeps_the_rest_of_a_suspended_batch_parked() -> None:
    async def gate(ctx: Ctx, call: ToolCallPart) -> ToolDecision:
        if call.tool_call_id == "c1":
            return Suspend({"pending_id": "approve-c1"})
        return Continue()

    runner = PolicyRunner(
        controls=ControlPlane(pre_tool_use=Permissions(gate)),
        rules_version="v1",
        subject="",
        resumed={},
        regate=(),
    )
    first = ToolCallPart("write", {"path": "a.txt"}, tool_call_id="c1")
    second = ToolCallPart("write", {"path": "b.txt"}, tool_call_id="c2")
    runner.begin_tool_round()
    first_decision = await runner.decide_tool(
        Ctx(turn=0), first, first.args_as_dict(), approved=False, recorded=False
    )
    second_decision = await runner.decide_tool(
        Ctx(turn=0), second, second.args_as_dict(), approved=False, recorded=False
    )
    assert isinstance(first_decision, Suspend)
    assert second_decision == Deny(NOT_EXECUTED)
```

Use real `Ctx`, `ToolCallPart`, and `ControlPlane` values. Assert concrete `Continue`, `Deny`,
and `Suspend` decisions. The existing assembled capability tests continue to assert their mapping
to `ApprovalRequired` and `SkipToolExecution`.

- [ ] **Step 2: Run the tests and verify import failure**

Run:

```bash
uv run --offline pytest -q tests/test_effect_boundary.py -k policy_runner
```

Expected: collection fails because `semora.policy` does not exist.

- [ ] **Step 3: Create the internal policy collaborator**

Create `PolicyRunner` with no store or transcript dependency. Its constructor is
`PolicyRunner(*, controls, rules_version, subject, resumed, regate)`. Its exact public-to-core
surface is `prepare_tools(tool_defs)`, `begin_tool_round()`,
`context(run_context, *, pending=None, tool=None)`,
`decide_tool(here, call, args, *, approved, recorded)`, `note_tool(call, *, refused)`, and
`after_tool(here, call, record)`. `decide_tool()` returns a Semora `ToolDecision`; conversion to
Pydantic `ApprovalRequired` or `SkipToolExecution` stays in `ExecutionBoundary`.

Move `_gate`, `_resume_decision`, `_ctx`, `_made`, `_approval_tools`,
`_suspended_this_round`, and `calls_made` into the collaborator. Move `Resumed` and
`NOT_EXECUTED` to `policy.py` and re-export both from `effects.py` so existing internal imports
remain stable.

- [ ] **Step 4: Make `ExecutionBoundary` coordinate policy and journal**

The Pydantic hook remains on `ExecutionBoundary`:

```python
async def before_tool_execute(
    self,
    ctx: RunContext[Any],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: Any,
) -> Any:
    here = self.policy.context(ctx, tool=tool_def)
    decision = await self.policy.decide_tool(
        here,
        call,
        args,
        approved=ctx.tool_call_approved,
        recorded=await self.journal.recorded(call.tool_call_id),
    )
    match decision:
        case Deny(result):
            self.policy.note_tool(call, refused=True)
            raise SkipToolExecution(result)
        case Suspend(request):
            if not request.get("pending_id"):
                raise ValueError(f"suspension of {call.tool_call_id!r} carries no pending_id")
            self.policy.note_tool(call, refused=True)
            raise ApprovalRequired(metadata=dict(request))
        case Continue():
            self.policy.note_tool(call, refused=False)
            return args
```

Use `self.policy.begin_tool_round()` at the start of each `CallToolsNode`, `self.policy.prepare_tools()` in `prepare_tools()`, and `self.policy.after_tool()` as the callback passed to `journal.project_once()`.

Keep these compatibility attributes on `ExecutionBoundary`, because `AgentRuntime._park()` reads them:

```python
self.controls = controls
self.rules_version = rules_version
self.subject = subject
```

Expose `calls_made` through a read-only property or alias so turn-level `Ctx` construction retains the same history.

- [ ] **Step 5: Update the internal layer order**

Use this order and resolve every upward import instead of suppressing the test:

```python
LAYERS = [
    "contracts",
    "ids",
    "controls",
    "policy",
    "transcript",
    "journal",
    "effects",
    "dispatch",
    "runtime",
]
```

If `journal.py` imports `transcript.stripped`, keep `transcript` before `journal`; `policy.py` must remain independent of `journal` and `effects`.

- [ ] **Step 6: Verify behavior and complexity reduction**

Run:

```bash
uv run --offline pytest -q \
  tests/test_effect_boundary.py \
  tests/test_hitl.py \
  tests/test_recovery.py \
  tests/test_packaging.py
uv run --offline ruff check packages/semora/src/semora tests
uv run --offline mypy
```

Also verify `ExecutionBoundary` no longer contains store state transitions or the implementations of `_gate`, `_resume_decision`, and `_journal_once`.

- [ ] **Step 7: Commit the policy extraction**

```bash
git add \
  packages/semora/src/semora/policy.py \
  packages/semora/src/semora/effects.py \
  tests/test_effect_boundary.py \
  tests/test_hitl.py \
  tests/test_packaging.py
git commit -m "refactor: isolate execution policy decisions"
```

---

### Task 5: Prove composition with public Pydantic durability surfaces

**Files:**

- Create: `tests/test_pydantic_durability.py`
- Modify only if required by a real failing integration test: `packages/semora/src/semora/runtime.py`

- [ ] **Step 1: Build a minimal public callable durability backend in the test**

Use public imports from `pydantic_ai.durable_exec` and record operation names without interpreting `cache_key`:

```python
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai.durable_exec import (
    BaseDurabilityCapability,
    DurabilityEngineSpec,
    DurableOperationId,
    JournalCallableOperationBackend,
    JSON_CODEC,
    RoleBasedOperationConfig,
)


class ImmediateBackend(JournalCallableOperationBackend[None]):
    def __init__(self, names: list[str]) -> None:
        super().__init__(
            agent_name="semora-composition",
            config=RoleBasedOperationConfig(
                model=None, event=None, capability=None, tool=None
            ),
        )
        self.names = names

    async def execute(
        self,
        *,
        operation_id: DurableOperationId,
        name: str,
        body: Callable[[], Awaitable[object]],
        cache_key: tuple[object, ...],
        config: None,
    ) -> object:
        self.names.append(name)
        return await body()


class ImmediateDurability(BaseDurabilityCapability[None]):
    engine_spec = DurabilityEngineSpec(
        engine_name="test",
        durable_unit_noun="operation",
        durable_container_noun="test run",
        codec=JSON_CODEC,
        unsupported_runtime_toolset_kinds=frozenset(),
        wrapped_toolset_kinds=frozenset(),
        toolset_lifecycles={},
        tool_call_result_upgrade_lenient=True,
        journal_discovery=True,
        sequential_tools_in_durable_context=False,
        tool_config_key=None,
    )

    def __init__(self, names: list[str], model: FunctionModel) -> None:
        super().__init__(models={"script": model}, name="semora-composition")
        self.backend = ImmediateBackend(names)

    def in_durable_context(self) -> bool:
        return True

    def get_durable_operation_backend(self) -> ImmediateBackend:
        return self.backend
```

If `JSON_CODEC` is not exported publicly by the installed minimum version, use the public codec export available in that version. Do not add a production dependency on a private module merely to make this test work.

- [ ] **Step 2: Add a native durability composition test**

Construct a named agent with `Agent(model, name="semora-composition")`, register that same
`FunctionModel` under `models={"script": model}`, pass the capability through
`AgentRuntime.run(capabilities=[durability])`, and assert:

```python
assert outcome.output == "done"
assert files.ran == ["memo.txt"]
assert any("model" in name for name in operation_names)
assert any("tool" in name for name in operation_names)
assert (await semora_store.read("compose", "tool:c00")).status == "done"
```

Run:

```bash
uv run --offline pytest -q tests/test_pydantic_durability.py -k immediate
```

Expected: pass without changing `AgentRuntime`, or fail with a concrete ordering/signal issue that is fixed at the existing capability-composition seam.

- [ ] **Step 3: Add a replay test with the second capability attached**

Run the tool once, then call `recover()` from the committed pending round while attaching a fresh `ImmediateDurability`. Assert the Semora `tool:` record is replayed and `files.ran` remains a one-item list. This test proves the extra capability does not bypass Semora's result-first record.

- [ ] **Step 4: Add a `StepPersistence` composition test**

Use the development dependency through its public API:

```python
from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence


async def test_runtime_composes_with_step_persistence() -> None:
    steps = InMemoryStepStore()
    capability = StepPersistence(store=steps, agent_name="semora-test")
    outcome = await AgentRuntime(MemorySteps()).run(
        "branch-1",
        agent,
        "write",
        conversation_id="conversation-1",
        capabilities=[capability],
    )
    runs = await steps.list_runs(conversation_id="conversation-1")
    assert outcome.output == "done"
    assert len(runs) == 1
    assert runs[0].run_id != "branch-1"
    events = await steps.list_events(run_id=runs[0].run_id)
    assert any(event.kind == "tool_call_completed" for event in events)
```

Use the actual public `EventKind` value from the installed harness. The identity assertion documents that Pydantic run identity and Semora branch identity are separate.

- [ ] **Step 5: Run composition plus runtime regressions**

Run:

```bash
uv run --offline pytest -q \
  tests/test_pydantic_durability.py \
  tests/test_effect_boundary.py \
  tests/test_recovery.py
```

Expected: all selected tests pass; ordinary tool errors remain model-visible results and Semora runtime signals are not converted into tool failures.

- [ ] **Step 6: Commit composition tests and any proven seam fix**

```bash
git add tests/test_pydantic_durability.py packages/semora/src/semora/runtime.py
git commit -m "test: prove pydantic durability composition"
```

If `runtime.py` did not change, omit it from `git add`.

---

### Task 6: Document ownership, persistence migration, and the backend readiness gate

**Files:**

- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/API.md`
- Modify: `docs/MIGRATION-PYDANTIC-NATIVE.md`
- Modify: `docs/PYDANTIC-NATIVE-SEMORA.md`

- [ ] **Step 1: Update the architecture explanation**

Describe the final boundary in `docs/PYDANTIC-NATIVE-SEMORA.md`:

```text
Pydantic AI owns the agent loop and generic durable-operation adapters.
Semora's outermost capability coordinates two internal services: PolicyRunner decides whether a
tool may cross the boundary, and EffectJournal records the result before the post-tool policy is
journaled. AgentRuntime owns the durable lifecycle between Pydantic runs.
```

Explain that `StepPersistence` is useful for Pydantic run snapshots and tool-effect observations, while Semora additionally owns approval routing, resume revalidation, leases/fencing, and fail-closed application-effect ambiguity.

- [ ] **Step 2: Document continuation compatibility in API and migration docs**

State all four facts explicitly:

1. New 0.5.x suspensions persist a JSON-compatible native `DeferredToolRequests` under `continuation.deferred`.
2. Resume uses `DeferredToolRequests.build_results()`.
3. Existing 0.5.x `continuation.calls` records remain readable.
4. The compatibility reader can be removed only in the next major release after a migration notice.

- [ ] **Step 3: Document why there is no Semora durable backend yet**

Record the readiness gate without presenting it as unfinished implementation:

```text
Semora does not derive durable effect identity from Pydantic's opaque cache-key tuple. A Semora
DurableOperationBackend becomes safe when Pydantic exposes a stable serializable invocation ID,
or when Semora has a transactional operation cursor. Until then, callers compose capabilities
through `AgentRuntime(capabilities=[capability])`.
```

Do not claim exactly-once external effects. Keep tool-call identity scoped to one run and leave cross-run business idempotency with the host.

- [ ] **Step 4: Add the change to `CHANGELOG.md` and a concise README pointer**

Mention native deferred persistence, internal boundary separation, and verified Pydantic capability composition. Keep internal class names out of the README quick-start unless they help explain ownership.

- [ ] **Step 5: Check documentation for stale architecture claims**

Run:

```bash
rg -n "DeferredToolResults\(|continuation.*calls|second agent|exactly.once|durable backend" \
  README.md CHANGELOG.md docs packages/semora/src/semora
```

Expected: every remaining match is either current API text, the 0.5.x compatibility note, or the explicit readiness gate.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md CHANGELOG.md docs/API.md docs/MIGRATION-PYDANTIC-NATIVE.md docs/PYDANTIC-NATIVE-SEMORA.md
git commit -m "docs: clarify pydantic durability ownership"
```

---

### Task 7: Verify the complete change and prepare it for review

**Files:**

- Modify only for defects found by verification.

- [ ] **Step 1: Inspect Python control-flow impact with CodeCanvas**

Analyze callers and impact for `ExecutionBoundary.wrap_tool_execute`, `AgentRuntime._finalize`, and `_decode_parked`. Confirm that every resume/recovery path crosses native request validation and that every tool path still crosses `EffectJournal.tool()` before post-tool projection.

- [ ] **Step 2: Run formatting and static checks**

```bash
uv run --offline ruff format .
uv run --offline ruff check .
uv run --offline ruff format --check .
uv run --offline mypy
```

Expected: all commands exit zero.

- [ ] **Step 3: Run the full test suite**

```bash
uv run --offline pytest -q
```

Expected: all non-PostgreSQL tests pass. Record the number of PostgreSQL tests skipped when `SEMORA_TEST_DSN` is absent; do not describe those skips as durable PostgreSQL verification.

- [ ] **Step 4: Build every package and execute both examples**

```bash
uv build --all-packages
uv run --offline python examples/permissions.py
uv run --offline python examples/recovery.py
```

Expected: package builds succeed and both examples exit zero with their documented output.

- [ ] **Step 5: Audit the final diff and dependency boundary**

```bash
base=$(git merge-base main HEAD)
git diff --check "$base" HEAD
git diff --stat "$base" HEAD
git status --short
```

Confirm no production import uses `pydantic_ai.durable_exec` private modules, no files under the main workspace's `issue_drafts` are present, and no `semora-store` file imports Pydantic.

- [ ] **Step 6: Commit verification fixes, if any**

```bash
git add -u
git commit -m "fix: resolve durability integration checks"
```

Skip this commit when verification required no code or documentation fixes.

- [ ] **Step 7: Perform the finishing-branch review**

Use the `superpowers:verification-before-completion` and `superpowers:finishing-a-development-branch` procedures. Report the exact commit range, test counts, skipped PostgreSQL coverage, build result, and any remaining limitation from the durable-backend readiness gate before merging or pushing.
