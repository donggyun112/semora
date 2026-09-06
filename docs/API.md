# Semora API

Semora extends Pydantic AI. Use its native `Agent`, `RunContext`, tool definitions, deferred results, and model messages throughout. See the [Pydantic-native migration](MIGRATION-PYDANTIC-NATIVE.md); applications migrating from 0.2 should also read the [0.3 migration](MIGRATION-0.3.md).

## Imports

```python
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from semora import (
    AgentRuntime,
    AgentSuspended,
    Answer,
    Continue,
    ControlPlane,
    ControlSignal,
    Ctx,
    Deny,
    Effects,
    ExecutionBoundary,
    FinishPolicy,
    Halt,
    Ingress,
    Journal,
    MemorySteps,
    MemoryTranscript,
    Outcome,
    PendingInput,
    Permissions,
    Proceed,
    Prompt,
    Recover,
    ResumeInput,
    Steering,
    Suspend,
    Suspending,
    gate,
    new_branch_id,
    writer,
)
from semora_store import (
    Contended,
    ConversationScopedSteps,
    ExecutionContext,
    ExecutionStore,
    Fenced,
    Indeterminate,
)
from semora_store_pg import PostgresSteps, PostgresTranscript
```

## Runtime

`AgentRuntime(execution_store=None, *, transcript=None, lease_ttl=60.0, retry_running=False)`

All methods below are async. `branch_id` accepts a string or `ExecutionContext`. A branch is the durable unit: a first loop plus its resumes and recoveries; a fork is a new branch. `ExecutionContext(branch_id, conversation_id=None)` also names the conversation the branch belongs to; the ledger is scoped by it, so a branch that ran inside a conversation is resumed, recovered and inspected by naming that conversation, either on the context or through the `conversation_id=` keyword every method accepts. Without a conversation nothing is scoped.

| Method | Result and contract |
|---|---|
| `run(branch_id, agent, prompt=None, *, controls=None, rules_version="", prompt_id=None, conversation_id=None, message_history=None, deferred_tool_results=None, deps=None, capabilities=(), **options)` | `Outcome`; `agent` implements Pydantic AI's public `AbstractAgent` interface. Extra model/run options reach that agent. Acquires and renews a run lease. |
| `resume(branch_id, pending_id, answer, agent, *, controls=None, rules_version="", deps=None, capabilities=(), **options)` | Records an answer, then revalidates when all parked calls are answered. Caller-supplied Pydantic capabilities and run options reach the resumed attempt. Unanswered siblings raise `AgentSuspended` again. Unknown pending IDs raise `LookupError`. |
| `recover(branch_id, agent, history, *, controls=None, rules_version="", conversation_id=None, deps=None, capabilities=(), **options)` | Continues from native message history with caller-supplied Pydantic capabilities and run options. Reuses committed effects; unreported effects raise `Indeterminate` unless retry was explicitly enabled. |
| `fork(source, at, target, agent, prompt=None, *, history=None, regate=False, controls=None, rules_version="", source_conversation_id=None, conversation_id=None, deps=None, **options)` | Starts `target` from `source`'s transcript at entry uuid `at` (`None`: the active tip), or from `history` when the host keeps its own coordinates. Effects the source finished in that history are copied to the new run's ledger and replay; `regate=True` asks the new run's `pre_tool_use` about each first, and only `Continue` replays. A call the source started and never reported is copied as started, so `retry_running` decides. The rest runs under the new run's policy. The source is never written. |
| `committed_history(branch_id, conversation_id=None)` | `list[ModelMessage]`; requires a transcript. Supply this to `recover`. |
| `submit(branch_id, item)` | Enqueues and returns a `PendingInput`; requires an execution store. |
| `dispatch(branch_id, agent, command, *, controls=None, **options)` | Routes `Prompt`, `Answer`, or `Recover` using durable state. Attach both ledger and transcript. |
| `pending(branch_id)` | Undecided `(pending_id, tool_call_id)` pairs in model order. |
| `state(branch_id)` | `fresh`, `interrupted`, `completed`, or suspension state `waiting`/`resuming`; without a transcript an unparked run is `idle`. |

`interrupted` includes a still-running worker. Only acquiring the lease distinguishes it from a dead one.

Each `run`, `resume`, or `recover` is a fresh Pydantic AI attempt. Reconstruct and pass security-sensitive capabilities on every entry from a replacement process. Semora deliberately persists data and decisions, never executable capability objects. `fork` and `dispatch` accept capabilities through their Pydantic `**options` passthrough.

Approval updates and finalization hold the run lease; concurrent `resume` calls may raise `Contended` before accepting the answer, so the host should retry that answer. A prompt submitted through `run` while a fully answered continuation is resuming is enqueued before `Contended` is raised. Keep a stable `prompt_id` on retries to avoid enqueueing it twice.

`Outcome` exposes `output`, `stop_reason`, optional native `result`, `pending`, `pending_id`, `suspended`, and `all_messages()`. A runtime-level park is raised as `AgentSuspended`, carrying `pending_id`, `tool_call_id` and ordered `pending` pairs.

The active suspension stores the complete native `DeferredToolRequests` value as JSON-compatible
data under `continuation.deferred`. Resume validates that value and the active call-ID list, then
uses `DeferredToolRequests.build_results()` to construct the next run's input. The reader also
accepts the earlier 0.5.x `continuation.calls` representation so a new worker can resume work
parked during a rolling upgrade. New writes use only `continuation.deferred`; malformed or
mismatched stored requests raise before the agent resumes. The legacy reader remains throughout
0.5.x and may be removed in the next major release.

Use `{"type": "approve"}` for approval, `{"type": "approve", "args": {...}}` to approve with replaced arguments, and `{"type": "error", "message": "declined"}` for refusal. Replaced arguments are validated by Pydantic AI and are what `on_resume` sees as the call; the original request stays in `ResumeInput.request`. Policy-version strings are host-provided labels.

A refusal is not routed through `on_resume` and cannot be lifted there. It ends the round: the outcome's `stop_reason` is `"aborted"`, the refusal message is that call's recorded result, every call the same round approved still runs, and the model is not asked again — handed a refusal it would call the tool again and the same person would answer the same prompt, without bound. Calls in the round that nobody has answered yet keep the run parked; only the answer that completes the round ends it. A host that wants the model to see a rejection and try something else expresses that as a `Deny` from `pre_tool_use`, which is a policy verdict rather than a person's.

## Durable control points

Use Pydantic AI capabilities and hooks, or Harness guardrails, for general input, model, tool, and output policy. `ControlPlane` carries the permission, revalidation, journal, and suspension decisions that participate in Semora's durable contract. It accepts any subset of the existing async functions. `Ctx` contains `turn`, native `messages`, `calls_made`, `text`, `subject`, and `tool`. A tool call is a native `ToolCallPart`: access `tool_name`, `tool_call_id`, `args_as_dict()`.

`Ctx.tool` is the native `ToolDefinition` behind the call at `pre_tool_use`, `on_resume` and `post_tool_use`, and `None` at every other point, `on_suspend` included. A call carries a name and arguments but never the tool's own declaration, so a permission class the host attached with Pydantic AI's `Tool(fn, metadata={"permission": "read"})` is read from `ctx.tool.metadata` and nowhere else.

`Ctx.run` is Pydantic AI's own `RunContext`, present at every control point the agent loop reaches and `None` only at `on_suspend`. The other fields are lifted out of it for the common case; this is the rest — `deps`, `usage`, `retries`, `tool_call_approved`, the tool manager — and it is what native helpers take, so a gate selects tools with `matches_tool_selector(selector, ctx.run, ctx.tool)` rather than a matching rule of its own. `ctx.run.tool_call_metadata` is not the parked request: Pydantic AI fills it on its inline deferred-handler path, which a durable park does not take. Read the request from `ResumeInput.request`.

| Point | Signature | Composition |
|---|---|---|
| `on_inputs` | `(ctx, list[PendingInput]) -> list[PendingInput] \| Halt` | `Ingress(*screens)` chains rewrites |
| `before_model` | `(ctx) -> Proceed \| Halt` | `Steering(*sources)` accumulates native request parts |
| `pre_tool_use` | `(ctx, call) -> Continue \| Deny \| Suspend` | `Permissions(*stages)`, denial outranks suspension |
| `post_tool_use` | `(ctx, call, result) -> None` | `Journal(*writers)` runs in order; may mutate a mutable result |
| `before_finish` | `(ctx, stop_reason) -> Proceed \| Halt` | `FinishPolicy(*gates)` combines steering or stops |
| `on_resume` | `(ctx, call, resume) -> Continue \| Deny \| Suspend` | Receives human input plus suspended/current version labels |
| `on_suspend` | `(ctx, call, request, snapshot, completed) -> None` | `Suspending(*persisters)` completes before durable park |

`Continue()` allows; `Deny(result)` gives the model a result without executing the tool; `Suspend({"pending_id": ...})` requests a durable pause. `Proceed(steers=())` continues with optional native request parts. `Halt(reason)` ends the attempt. `PendingInput(kind, part, origin_id=None)` holds a native request part. `ResumeInput` contains `answer`, `request`, `suspended_rules_version`, `current_rules_version`.

`gate(fn)` adapts a call-only function returning `None` or an error/suspend result. `writer(fn)` adapts a call/result-only journal function.

## Effect and storage contract

`ExecutionBoundary(store, branch_id, token=0, *, controls=None, retry_running=False, rules_version="", subject="", resumed=None, inputs=None, record=None)` is the capability the runtime installs per attempt. `Effects` is a compatibility alias for the same class. Prefer `AgentRuntime` so lease and continuation ownership stay centralized.

The runtime places this boundary outermost and then installs caller-supplied Pydantic
`capabilities=` inside it. Public Pydantic durability capabilities and Harness `StepPersistence`
can observe or durably wrap their own operations without replacing Semora's result-bearing effect
record, approval routing, lease, or fencing rules. A Pydantic `run_id` identifies one
`Agent.run()` call; a Semora `branch_id` spans its run, resume, and recovery attempts.

- `tool:{call_id}` stores the original effect result envelope. Ordinary tool exceptions produce committed error results. `ControlSignal`, ledger signals and cancellation propagate.
- `after:{call_id}` stores the completed model-visible journal projection separately. Recovery reuses that projection without leaking the unredacted original. A journal can execute again if the process dies before committing its projection: external journal effects must be idempotent.
- `running` without a result is indeterminate. Fencing protects ledger writes, not arbitrary external APIs. A forced retry needs the host's idempotency/reconciliation contract.
- Run-scoped keys do not deduplicate a business operation across runs. The console supplies stable request/customer keys for its simulated payment separately.
- `MemorySteps` and `MemoryTranscript` do not survive a process restart. PostgreSQL adapters use an async psycopg pool and share the same protocols; see the conformance tests for setup and behavior.

Semora does not currently provide a `BaseDurabilityCapability` backend. Pydantic's backend
`cache_key` is an opaque tuple containing live runtime objects for relevant operations, so Semora
does not derive durable identity from tuple positions or `repr()`. Such a backend requires a
stable serializable invocation identity from Pydantic or a Semora-owned transactional operation
cursor. Compose existing durability capabilities through `AgentRuntime(capabilities=[capability])` in
the meantime.

`Contended` means another worker holds the run lease. `Fenced` means a stale writer's token was rejected. `Indeterminate` includes `branch_id` and `step`. `InvalidTransition` (from `semora.dispatch`) carries the observed `state` and rejected `command`. Do not convert these signals into ordinary tool errors in host adapters.
