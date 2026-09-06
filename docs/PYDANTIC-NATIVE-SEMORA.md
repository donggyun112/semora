# Pydantic-native Semora

## Goal

Semora uses Pydantic AI's public execution model directly and adds the application-effect
guarantees that Pydantic AI and Pydantic AI Harness do not impose. Pydantic AI owns agents,
messages, tool definitions, dependency injection, model calls, the agent graph, deferred tools,
and generic durability adapters.

Semora owns fail-closed effect recovery, lease and fencing enforcement, durable approval routing,
policy revalidation, and branch lifecycle across separate Pydantic runs.

## Ownership boundary

### Pydantic AI owns

- `Agent`, `RunContext`, model messages, tool calls, tool definitions, and argument validation.
- The graph that performs model requests, selects tools, invokes them, and produces output.
- Deferred execution through `ApprovalRequired`, `DeferredToolRequests`,
  `DeferredToolResults`, `ToolApproved`, and `ToolDenied`.
- Generic lifecycle and durable-engine integration through capabilities and
  `BaseDurabilityCapability`.
- Conversation, run, and tool-call identity at their native scopes.

Semora does not introduce equivalent message classes, tool-call classes, dependency containers,
agent loops, or deferred-result envelopes.

### Semora owns

- A result-bearing effect record written before post-tool policy is journaled.
- `Indeterminate` as the default response to an intent without a committed result.
- Explicit caller opt-in before retrying an indeterminate effect.
- Branch leases, fencing tokens, and stale-writer rejection.
- Durable pending inputs and atomic transitions among running, suspended, and completed states.
- Revalidation of an approval under the current policy and policy-version label.
- Forking that preserves completed and indeterminate effect knowledge, with optional re-gating.
- The dependency-free `semora-store` contract and its PostgreSQL implementation.

## Runtime architecture

Applications construct a native Pydantic agent and give it to `AgentRuntime`:

```python
from pydantic_ai import Agent
from semora import AgentRuntime, MemorySteps

agent = Agent(model)
runtime = AgentRuntime(MemorySteps())
outcome = await runtime.run("branch-1", agent, "do the work")
```

`AgentRuntime` acquires a branch lease, loads native message history, installs one
`ExecutionBoundary`, and calls `AbstractAgent.run()`. Each resume or recovery is a new Pydantic
run with a new `run_id`; the same `conversation_id` connects those attempts. The Semora
`branch_id` is the durable execution coordinate across attempts.

`ExecutionBoundary` remains one outermost Pydantic capability. It coordinates two internal
services:

- `PolicyRunner` composes `Continue`, `Deny`, and `Suspend`, revalidates approved calls, and
  produces the model-visible post-tool projection.
- `EffectJournal` owns model/tool keys, `absent`/`running`/`done` transitions, committed-result
  replay, and `Indeterminate`.

These are internal collaborators rather than independently ordered capabilities. Keeping one
outer catch boundary preserves the required order: the tool result is committed first, then
post-tool policy runs, then its projection marker is stored. Runtime signals cross that boundary
without becoming ordinary tool failures.

The existing `Effects` name remains an alias for `ExecutionBoundary`. Semora exports no Agent
class or tool decorator.

## Execution flow

### Fresh tool call

1. Pydantic AI produces and validates a native `ToolCallPart`.
2. `PolicyRunner` evaluates `pre_tool_use`. `Deny` becomes a model-visible result and `Suspend`
   becomes Pydantic's `ApprovalRequired`.
3. `EffectJournal` checks `tool:{tool_call_id}`.
4. A completed record replays. A running record raises `Indeterminate` unless the caller set
   `retry_running=True`. An absent record commits intent before calling the tool.
5. An ordinary tool return or error is committed as the effect result.
6. Post-tool policy runs against a copy and `after:{tool_call_id}` stores that model-visible
   projection.
7. Pydantic AI continues its graph with the result.

An external effect is not exactly once. A crash can happen after the external service accepted a
request and before Semora stored the result. Retrying requires an idempotency or reconciliation
contract owned by the host and external service.

### Suspension and resume

1. `Suspend({"pending_id": value})` becomes `ApprovalRequired` metadata.
2. Pydantic AI returns a native `DeferredToolRequests` batch.
3. `AgentRuntime` stores the complete native batch in JSON-compatible form together with message
   history, subject, policy version, and branch suspension state, then releases the worker.
4. The host records answers by `pending_id`. A batch remains parked until every call is answered.
5. Approved calls cross `on_resume` under the current policy. Argument replacements are the
   native call arguments seen by that control point.
6. `DeferredToolRequests.build_results()` validates call identity and constructs the native
   `DeferredToolResults` supplied to the next Pydantic run.

New 0.5.x records use `continuation.deferred`. Semora still reads the earlier
`continuation.calls` shape throughout 0.5.x so a deployment can resume work parked by an older
worker. Malformed values or call IDs that differ from the active suspension fail before a tool
runs. The legacy reader can be removed no earlier than the next major release.

A human refusal retains Semora's round-abort behavior: approved siblings run, the refusal is the
denied call's result, and the model is not called again for that round. A policy `Deny` is the path
for a rejection the model should observe and route around.

### Recovery and fork

Recovery supplies Pydantic-native message history. Completed effects replay their stored result;
incomplete intent remains indeterminate. A fork copies effect knowledge into a new Semora branch
and can ask the new branch's policy to re-gate copied records.

Tool-call identity belongs to one Pydantic run. Cross-run business identity belongs to the host;
the host must supply its own stable key when an operation must deduplicate across branches or
conversations.

## Composing Pydantic capabilities

Every runtime entry accepts `capabilities=` and installs caller capabilities beside Semora's
outermost boundary. Tests cover a public `BaseDurabilityCapability` implementation and Harness
`StepPersistence`.

`StepPersistence` records Pydantic run events, snapshots, and tool-effect status. It is useful for
observing or continuing a Pydantic run, but its documented contract does not restore arbitrary
capability state or automatically deduplicate external side effects. A Harness `run_id` remains
one `Agent.run()` identity and is deliberately different from Semora's `branch_id`.

Semora's ledger remains authoritative for result-bearing replay, approval routing, worker leases,
fencing, and fail-closed ambiguity. Harness stays a development dependency; `semora-store` has no
Pydantic or Harness dependency.

## Why Semora is not a Pydantic durable backend yet

Pydantic's public backend builder passes an operation ID, operation name, async body, engine
configuration, and an opaque `cache_key`. In Pydantic AI 2.40 that cache key contains live
`RunContext`, model, tool, and function objects for relevant operations. It is not a stable,
generic JSON identity. Depending on tuple positions, hashing `repr()`, or serializing live
functions could associate a replay with the wrong application effect after an upgrade or process
change.

A production `SemoraDurability(BaseDurabilityCapability)` becomes safe when either:

1. Pydantic exposes a stable, serializable per-invocation identity to the backend; or
2. Semora adds a transactional operation cursor that resumes from a committed transcript
   frontier without renumbering operations.

Until one condition holds, Semora composes with Pydantic durability capabilities through
`AgentRuntime(capabilities=[capability])`. It does not inspect Pydantic private tuple layouts or
claim a generic durable backend it cannot identify safely.

## Policy integration

General input, model, tool, and output policy belongs in Pydantic capabilities, hooks, or Harness
guardrails. `ControlPlane` remains for decisions coupled to Semora's durable contract:

- `pre_tool_use` authorizes, denies, or durably suspends an effect;
- `on_resume` revalidates an approved call;
- `post_tool_use` projects a committed result and is journaled per call;
- `on_suspend` observes the complete batch before the durable park is announced;
- the input/model/finish controls participate in Semora's durable input and transcript flow.

Applications reconstruct executable controls and other capabilities in a replacement process.
Semora persists native messages and decisions, never Python callables.

## Testing

Behavior tests are the authority for Semora guarantees. They prove native deferred serialization,
0.5.x continuation compatibility, approval argument replacement, result replay, unresolved-effect
failure, Pydantic capability composition, Harness observation, lease/fencing behavior, and store
layer independence.

The full checks are `ruff check`, `ruff format --check`, `mypy`, `pytest`, and
`uv build --all-packages`. PostgreSQL conformance requires `SEMORA_TEST_DSN` and is reported
separately when unavailable.
