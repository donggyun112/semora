# Migrating to the Pydantic-native boundary

Semora now names its Pydantic AI capability `ExecutionBoundary`. The older `Effects` import remains
an identity alias during the migration.

```python
from semora import Effects, ExecutionBoundary

assert Effects is ExecutionBoundary
```

## Use the native agent

Applications should construct `pydantic_ai.Agent` directly and let `AgentRuntime` attach Semora's
boundary for each attempt:

```python
from pydantic_ai import Agent
from semora import AgentRuntime

agent = Agent(model, tools=[write_file])
runtime = AgentRuntime(store, transcript=transcript)
outcome = await runtime.run("branch-1", agent, "update the file")
```

The former `semora.Agent` subclass and `semora.tool` decorator have been removed. Replace their
class-body configuration with Pydantic AI's constructor, tools, capabilities, and dependency
injection. Keep branch identity and durable lifecycle calls on `AgentRuntime`:

```python
from pydantic_ai import Agent, RunContext
from semora import AgentRuntime, MemorySteps


async def write(ctx: RunContext[AppDeps], path: str, text: str) -> str:
    return await ctx.deps.files.write(path, text)


agent = Agent(model, deps_type=AppDeps, tools=[write])
runtime = AgentRuntime(MemorySteps())
outcome = await runtime.run("branch-1", agent, "update the file", deps=deps)
```

An `AgentSuspended` exception is the durable park signal. Route its `pending_id` to the host and
call `runtime.resume(...)` when an answer arrives. Semora no longer converts that signal into a
special run-bound Agent outcome.

## Direct capability use

A host that owns leases and continuation storage can install the boundary on a native run:

```python
from pydantic_ai import Agent
from semora import ExecutionBoundary

agent = Agent(model, tools=[write_file])
result = await agent.run(
    "update the file",
    capabilities=[ExecutionBoundary(store, "branch-1")],
)
```

Use `AgentRuntime` when Semora must own lease renewal, durable suspension, host-input routing,
transcripts, recovery, or fork.

## Rebuild capabilities on continuation

Pydantic capabilities are executable application objects and are not persisted. Pass the current
capability set whenever a new process resumes or recovers a branch:

```python
capabilities = [security_guardrails, step_persistence]

await runtime.resume(
    "branch-1",
    pending_id,
    {"type": "approve"},
    agent,
    capabilities=capabilities,
)

await runtime.recover(
    "branch-1",
    agent,
    committed_history,
    capabilities=capabilities,
)
```

The same rule applies to model settings, usage limits, toolsets, and other Pydantic run options.
`run`, `resume`, and `recover` forward them to the native `Agent.run` attempt. `fork` and `dispatch`
accept them through `**options`.

## Move general policy to Pydantic

Keep Semora controls where policy participates in its durable contract:

- permission requests that must become a durable suspension;
- revalidation after an approval;
- result projection that must replay from Semora's journal;
- suspension persistence tied to the branch transition.

Use Pydantic AI capabilities and hooks, or Pydantic AI Harness guardrails, for ordinary input,
model, tool, and output policy. `ControlPlane` remains for controls that participate in durable
input admission, permission decisions, approval revalidation, result journaling, and suspension
commits. Pass it explicitly to the runtime; policy methods are no longer discovered from an Agent
subclass.

Semora still does not promise exactly-once external effects. A started call without a committed
result is `Indeterminate` by default, and retry requires the host's explicit idempotency or
reconciliation contract.
