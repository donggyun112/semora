# Pydantic AI durability integration design

**Status:** proposed

## Context

Semora now accepts `pydantic_ai.agent.AbstractAgent` and delegates the agent loop to
`AbstractAgent.run()`. The remaining core still performs three jobs inside one
`ExecutionBoundary` capability and one `AgentRuntime` service:

1. policy decisions around inputs, model requests, tool calls, resume, and finish;
2. durable recording and replay of model and tool effects;
3. branch lifecycle orchestration such as leases, suspension, recovery, and forks.

Pydantic AI 2.40 provides three public integration surfaces relevant to these jobs:

- native deferred requests and `DeferredToolRequests.build_results()`;
- `AbstractCapability` lifecycle hooks;
- `BaseDurabilityCapability` with a `DurableOperationBackend` for model, toolset, event,
  compaction, and capability operations.

Pydantic AI Harness also provides `StepPersistence`. It records events, snapshots, and
started/completed/failed tool effects. Its documented contract deliberately does not restore
capability state or deduplicate replayed external side effects. Pydantic's durable backend guide
likewise states that a durable unit can run more than once around a worker failure. Semora's
lease, fencing, result-first commit, revalidation, and `Indeterminate` rules therefore remain
useful execution semantics.

## Goals

- Use native Pydantic deferred request validation instead of rebuilding approval results by hand.
- Keep Semora on Pydantic's public capability and durability extension points.
- Make the policy, effect journal, and branch lifecycle boundaries explicit in the code.
- Prove composition with another Pydantic durability capability and with `StepPersistence`.
- Preserve Semora's existing fail-closed recovery behavior and public store independence.
- Establish a safe gate for a future Semora `DurableOperationBackend` without depending on
  Pydantic private tuple layouts.

## Non-goals

- Reimplement the Pydantic agent graph, tool manager, message types, or tool registry.
- Replace `semora-store` with `pydantic-ai-harness` storage.
- Claim exactly-once execution for an external effect.
- Treat an approval response as unconditional authorization on resume.
- Add Temporal, DBOS, Prefect, or another workflow engine as a required dependency.

## Decision

The next release will deepen Pydantic integration in two mergeable slices. A Semora durable
backend will remain behind an explicit readiness gate until it can derive stable invocation
identity from a public Pydantic contract.

### 1. Native deferred continuation

The persisted continuation will store the complete native `DeferredToolRequests` value, encoded
with a Pydantic `TypeAdapter`, alongside Semora's policy metadata. Resume and recovery will rebuild
that value and call `requests.build_results(approvals=...)`.

This changes ownership as follows:

- Pydantic validates that every answered call ID belongs to the parked request and is an approval.
- Pydantic constructs `DeferredToolResults` and applies argument overrides.
- Semora still maps host answers to `ToolApproved` or `ToolDenied`, persists answer state, runs
  `on_resume`, and decides whether the attempt is aborted after a denial.

Old in-flight continuations containing Semora's existing `calls` list remain readable. New writes
use the native field. The compatibility reader remains throughout 0.5.x and can be removed no
earlier than the next major release, with the removal called out in its migration guide.

### 2. Capability composition and internal separation

`ExecutionBoundary` remains the single outermost Pydantic capability because result-first commit
and post-tool policy ordering form one recovery contract. Splitting them into independently
ordered capabilities would create a crash window between the effect commit and the journal marker.

Its implementation will be separated behind two internal collaborators:

- `EffectJournal` owns model/tool keys, absent/running/done transitions, replay, and
  `Indeterminate`.
- `PolicyRunner` owns `Continue`, `Deny`, `Suspend`, resume revalidation, and post-tool projection.

`ExecutionBoundary` coordinates those collaborators through Pydantic hooks. It stays outermost so
runtime signals cross one catch boundary. This refactor is accepted only if it reduces the
complexity of `ExecutionBoundary` and keeps the result commit before post-tool journaling.

Tests will attach a second Pydantic durability capability and `StepPersistence` to the same native
agent. They will prove that:

- Semora still records one logical effect;
- a committed result is replayed without invoking the tool body again;
- an unresolved running effect remains `Indeterminate` by default;
- Pydantic run and conversation IDs remain distinct from Semora branch and business identity;
- capability order does not swallow Semora runtime signals.

## Durable backend readiness gate

The public backend builder passes `operation_id`, a persisted operation name, an async body, an
opaque `cache_key`, and engine configuration to `CallableOperationBackend.execute()`. A probe
against Pydantic AI 2.40 found that the cache key contains live `RunContext`, model, tool, and
function objects. It cannot be encoded with `JSON_CODEC` as a generic tuple. The tuple includes a
tool call ID today, but its position is not a documented public contract.

Semora must not hash `repr(cache_key)`, pickle live functions, or inspect undocumented tuple
positions. Those choices could replay the wrong effect after a dependency, process, or Pydantic
upgrade changes the representation.

A production `SemoraDurability(BaseDurabilityCapability)` can be implemented when one of these
conditions is met:

1. Pydantic exposes a stable, serializable per-invocation identity to backend `execute()`; or
2. Semora adds a transactional operation cursor that can resume from a committed transcript
   frontier without renumbering operations.

The first option is preferred. Until then, callers may compose Semora with Pydantic's official
durability capabilities through `AgentRuntime.run(capabilities=[...])`; Semora remains the outer
application-effect boundary.

## Data flow

### Fresh run

1. `AgentRuntime` acquires a branch lease and fencing token.
2. It opens the native Pydantic message history from the transcript.
3. It installs `ExecutionBoundary` before caller capabilities.
4. Pydantic owns model requests, tool selection, validation, execution, and the loop.
5. `EffectJournal` commits a tool result before `PolicyRunner` invokes `post_tool_use`.
6. The settled Pydantic messages are appended to the transcript.

### Suspension

1. `PolicyRunner` converts `Suspend` into Pydantic `ApprovalRequired` metadata.
2. Pydantic returns one native `DeferredToolRequests` batch.
3. `AgentRuntime` persists that native batch, the committed history, policy version, and subject in
   one Semora control transition.
4. The worker releases the lease and raises `AgentSuspended` to the host.

### Resume

1. The host stores an answer against the pending call ID.
2. Semora reloads and validates the native deferred request batch.
3. `on_resume` rechecks the approved call under the current policy and arguments.
4. `DeferredToolRequests.build_results()` creates the Pydantic resume input.
5. A new Pydantic run uses the same `conversation_id`, prior native history, and the deferred
   results. It receives a new Pydantic `run_id`.

### Recovery

1. The new worker acquires a newer fencing token.
2. Done effects replay from the Semora record.
3. A started effect without a terminal record raises `Indeterminate` unless the caller opted into
   retry under an external idempotency contract.
4. A stale worker cannot commit with its older token.

## Error handling

- Unknown or mismatched deferred call IDs fail before the agent resumes.
- Pydantic control-flow signals continue through the single capability boundary.
- Ordinary tool exceptions become committed error results visible to the model.
- Cancellation after a tool body and before its result commit leaves the effect running and
  therefore indeterminate.
- Legacy continuation decoding is fail-closed: malformed stored data raises instead of silently
  constructing a different approval batch.

## Public API and compatibility

No new agent class or decorator will be introduced. Applications continue to construct native
`pydantic_ai.Agent` objects and pass them to `AgentRuntime`.

The first slice requires no public signature changes. The native continuation shape is persisted
data and must be described in the migration documentation. Internal collaborator classes remain
private until a use case requires a public extension point.

The implementation is tested against the existing `pydantic-ai-slim>=2.38,<3` range. The minimum
version is raised only if a public API used by the final code is absent from 2.38; a development
test that targets a newer optional capability is version-gated instead of silently changing the
core installation floor.

The future durable backend, if the readiness gate is satisfied, will be an additional capability;
it will not replace `AgentRuntime` or silently change recovery behavior.

## Verification

- Focused tests for native deferred serialization, invalid IDs, denials, argument overrides, and
  legacy continuation reads.
- Existing HITL, recovery, concurrency, fencing, fork, transcript, and packaging suites.
- Composition tests with a minimal public `BaseDurabilityCapability` implementation.
- Composition tests with `pydantic_ai_harness.StepPersistence` as a development-only dependency.
- Ruff, formatting, mypy, full pytest, package builds, and both examples.
- PostgreSQL conformance when `SEMORA_TEST_DSN` is available; skipped PostgreSQL tests are reported
  as unverified rather than passed.

## Portfolio narrative

The project becomes a focused reliability layer for native Pydantic agents. Pydantic owns the
agent loop and generic durable-operation integration. Semora demonstrates the harder application
boundary: durable human approval, result-first effect records, fail-closed ambiguity, and
lease/fencing behavior across workers. The code shows integration judgment instead of presenting
a second agent framework with similar names.
