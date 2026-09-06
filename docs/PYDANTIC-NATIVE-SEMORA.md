# Pydantic-native Semora

## Goal

Semora shall use Pydantic AI's public execution model directly and add only the durable guarantees
that Pydantic AI and Pydantic AI Harness do not provide. Pydantic AI remains the owner of agents,
messages, tool definitions, dependency injection, model calls, the agent graph, lifecycle hooks,
guardrails, and deferred-tool result types.

Semora remains the owner of fail-closed external-effect recovery, lease and fencing enforcement,
approval revalidation, atomic host-input transitions, and effect-aware branch recovery.

## Decision

The migration will use Pydantic AI Core immediately and place Pydantic AI Harness behind a narrow,
optional adapter boundary.

Harness 0.29 records run events, snapshots, and tool-effect states, but its tool-effect record does
not contain the tool result needed to replay a completed external effect. Its store protocol also
does not define Semora's lease, fencing, pending-input, or atomic transition operations. Making it
the only source of truth now would weaken Semora's recovery contract.

Semora will therefore retain its execution store until an upstream public protocol can express the
same guarantees. Harness integration will reuse its public `StepStore` types for observation and
continuation without making Semora depend on Harness internals.

## Ownership boundary

### Pydantic AI owns

- `Agent`, `RunContext`, model messages, tool calls, tool definitions, and tool argument validation.
- The agent graph and all model/tool invocation.
- Deferred execution through `ApprovalRequired`, `DeferredToolRequests`,
  `DeferredToolResults`, `ToolApproved`, and `ToolDenied`.
- General lifecycle behavior through public hooks, capabilities, and guardrails.
- Conversation, run, and tool-call identity at their native scopes.

Semora must not introduce equivalent message classes, tool-call classes, dependency containers,
agent loops, or deferred-result envelopes.

### Semora owns

- A result-bearing effect record written before post-tool journaling.
- `Indeterminate` as the default response to an intent without a committed result.
- Explicit opt-in before retrying an indeterminate effect.
- Branch leases, fencing tokens, and stale-writer rejection.
- Durable pending inputs and atomic transitions between running, suspended, and completed states.
- Revalidation of an approval against the current policy and its version.
- Forking that preserves completed and indeterminate effect knowledge, with optional re-gating.
- The dependency-free `semora-store` contract and its PostgreSQL implementation.

## Public architecture

The primary integration surface will be a Pydantic AI capability installed on a native
`pydantic_ai.Agent`:

```python
from pydantic_ai import Agent
from semora import AgentRuntime, ExecutionBoundary

agent = Agent(model)
runtime = AgentRuntime(store)
result = await runtime.run(
    execution,
    agent,
    prompt,
    capabilities=[ExecutionBoundary(policy=policy)],
)
```

`AgentRuntime` remains the coordinator that acquires a lease and supplies durable continuation
state. It delegates every agent step to `Agent.run`. `ExecutionBoundary` is the narrow capability
that adds Semora's effect and revalidation contract.

The existing `Effects` name becomes a compatibility alias during the migration. The optional
`semora.Agent` subclass and `@semora.tool` wrapper are deprecated after native-agent examples and
equivalent runtime ergonomics exist. They must not gain features that mirror Pydantic AI.

## Policy integration

General input, model, tool, and output policy belongs in Pydantic AI hooks or Harness guardrails.
Semora will stop presenting `ControlPlane` as a general policy framework.

Semora retains only callbacks that participate in its durable contract:

- `authorize_effect` decides whether an external effect may run or must become a native deferred
  request.
- `revalidate_approval` receives the original request, the supplied answer, the suspended policy
  version, and the current policy version.
- `project_effect_result` creates the model-visible result after the original result is committed.
- `on_suspend_committed` runs after suspension state is durable and cannot decide whether the tool
  executes.

Compatibility adapters map the old `pre_tool_use`, `on_resume`, `post_tool_use`, and `on_suspend`
callbacks to those roles. The other old control points migrate to Pydantic hooks and are removed in
the next major release. Pydantic's native denial and deferred-result types flow through unchanged.

## Execution flow

### New run

1. `AgentRuntime` acquires the branch lease and fencing token.
2. Host input is admitted through the execution store.
3. `AgentRuntime` calls native `Agent.run`, installing `ExecutionBoundary` alongside caller-supplied
   Pydantic capabilities.
4. Pydantic AI performs the model call and validates tool arguments.
5. `ExecutionBoundary` checks the result-bearing effect record.
6. A completed record returns its committed value. A running record raises `Indeterminate` unless
   the caller explicitly allowed retry. An absent record records intent before invoking the tool.
7. The tool result or tool error is committed before the model-visible projection is produced.
8. Pydantic AI continues its own graph.

### Suspension and resume

1. An authorization callback requests suspension by raising Pydantic's `ApprovalRequired` with
   Semora metadata containing the durable pending identity and policy version.
2. Pydantic returns `DeferredToolRequests`.
3. `AgentRuntime` atomically stores the pending requests and releases the worker lease.
4. `resume` records the host answer while holding a new lease.
5. Once the whole deferred batch is answered, Semora revalidates approvals against the current
   policy.
6. Semora constructs native `DeferredToolResults`; Pydantic AI validates replaced arguments and
   resumes the graph.

Human refusal retains Semora's explicit round-abort behavior. It is a host decision and is not sent
back to the model as permission-policy feedback.

### Recovery and fork

Recovery supplies Pydantic-native message history. A completed Semora effect is replayed from its
stored result; an incomplete intent remains indeterminate. Fork copies effect knowledge into the
new run identity and may revalidate copied effects before replay.

Harness `continue_run` and `fork_run` may supply snapshot history through an adapter, but they do
not replace the effect-copy or lease rules.

## Harness adapter

The adapter is optional and imports `pydantic-ai-harness` only when installed. It has two jobs:

1. expose a Semora execution as Harness run metadata and snapshots;
2. translate a Harness continuation snapshot into native `message_history` accepted by
   `AgentRuntime`.

The adapter never treats Harness `completed` as proof that a result value is replayable. Semora's
result-bearing effect record remains authoritative for that decision. Harness and Semora run IDs
must be mapped explicitly because Pydantic run identity is per `Agent.run`, while a Semora branch
can span multiple attempts.

No code may import Harness private modules such as `_capability`, `_store`, or `_types`.

## Compatibility and release sequence

This is a staged breaking migration.

1. Introduce `ExecutionBoundary`, native-agent documentation, and a compatibility adapter for the
   four durable `ControlPlane` callbacks. Keep current behavior and tests.
2. Add optional Harness snapshot interop and a PostgreSQL `StepStore` implementation in a separate
   package boundary. Do not duplicate Semora effect rows.
3. Deprecate the optional `semora.Agent`, `@semora.tool`, and general-purpose control points. Add
   migration examples using `pydantic_ai.Agent`, native tools, hooks, and guardrails.
4. In the next major release, remove the compatibility layer. Keep only the execution boundary,
   runtime coordinator, and storage contracts.

Every public API change updates `docs/API.md` and the migration guide. Deprecations include a
replacement example and remain for one minor release before removal unless maintaining them would
break the execution guarantee.

## Testing

Behavior tests remain the authority for Semora guarantees. The migration adds tests that prove:

- a native `pydantic_ai.Agent` uses `ExecutionBoundary` without `semora.Agent`;
- Pydantic deferred request/result types are preserved end to end;
- completed effects replay their committed result and incomplete effects fail closed;
- approval revalidation sees both policy versions and replaced arguments;
- caller-supplied Pydantic hooks and capabilities compose with Semora;
- Harness is optional at import time;
- the Harness adapter continues from snapshot messages without treating effect status as a stored
  result;
- memory and PostgreSQL stores pass the same lease, fencing, transition, and replay contract.

The full project checks remain `ruff check`, `ruff format --check`, `mypy`, `pytest`, and
`uv build --all-packages`. PostgreSQL conformance requires `SEMORA_TEST_DSN` and is reported
separately when unavailable.

## Non-goals

- Semora will not provide another agent loop or model abstraction.
- Semora will not wrap every Pydantic hook or guardrail behind its own vocabulary.
- Semora will not claim exactly-once external effects.
- Semora will not silently retry an incomplete effect.
- Semora will not depend on Harness private APIs or make Harness 0.x the authority for result
  replay.
