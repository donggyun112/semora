"""Recover a native Pydantic AI agent after a worker dies during a tool call.

    uv run python examples/reviewer.py

Pydantic AI owns the agent, dependencies, tools, and loop. Semora owns the ledger, transcript,
lease, approval revalidation, and the decision to retry an indeterminate test command.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from semora import (
    AgentRuntime,
    AgentSuspended,
    ControlPlane,
    MemorySteps,
    MemoryTranscript,
    Recover,
)
from semora.controls import Continue, Ctx, ResumeInput, Suspend, ToolDecision


@dataclass
class ReviewerDeps:
    repo: Path
    touched: list[str]
    hold: asyncio.Event | None = None


def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart("run_tests", {}, tool_call_id="c1"),
                ToolCallPart(
                    "write",
                    {"path": "REVIEW.md", "text": "LGTM"},
                    tool_call_id="c2",
                ),
            ]
        )
    return ModelResponse(parts=[TextPart("Reviewed. Tests pass; notes are in REVIEW.md.")])


def instructions(ctx: RunContext[ReviewerDeps]) -> str:
    return f"Review the repository at {ctx.deps.repo}. Run the tests before you judge."


async def run_tests(ctx: RunContext[ReviewerDeps]) -> str:
    """Run the test suite."""
    if ctx.deps.hold is not None:
        await ctx.deps.hold.wait()
    return "42 passed"


async def write(ctx: RunContext[ReviewerDeps], path: str, text: str) -> str:
    """Write one review file."""
    ctx.deps.touched.append(path)
    return f"wrote {path}"


async def authorize(ctx: Ctx, call: ToolCallPart) -> ToolDecision:
    if call.tool_name == "write":
        return Suspend({"pending_id": f"approve-{call.tool_call_id}"})
    return Continue()


async def revalidate(ctx: Ctx, call: ToolCallPart, resume: ResumeInput) -> ToolDecision:
    return Continue()


async def ask_person(pending_id: str) -> dict[str, str]:
    print(f"  a person is asked about {pending_id} ... approved")
    return {"type": "approve"}


async def drive(
    branch_id: str,
    agent: Agent[ReviewerDeps, str],
    runtime: AgentRuntime,
    deps: ReviewerDeps,
    controls: ControlPlane,
) -> None:
    """Carry the durable run from its current state until it completes."""
    state = await runtime.state(branch_id)
    print(f"{branch_id} is {state}")

    pending_id: str | None = None
    while True:
        try:
            if pending_id is not None:
                outcome = await runtime.resume(
                    branch_id,
                    pending_id,
                    await ask_person(pending_id),
                    agent,
                    controls=controls,
                    deps=deps,
                )
            elif state == "interrupted":
                outcome = await runtime.dispatch(
                    branch_id, agent, Recover(), controls=controls, deps=deps
                )
            elif state == "waiting":
                pending_id = (await runtime.pending(branch_id))[0][0]
                continue
            else:
                outcome = await runtime.run(
                    branch_id,
                    agent,
                    "review this repository",
                    controls=controls,
                    deps=deps,
                )
        except AgentSuspended as parked:
            pending_id = parked.pending_id
            state = "waiting"
            continue
        break

    print(f"  answer: {outcome.output!r}; written: {deps.touched}")


async def demo() -> None:
    store, transcript = MemorySteps(), MemoryTranscript()
    controls = ControlPlane(pre_tool_use=authorize, on_resume=revalidate)
    agent = Agent(
        FunctionModel(scripted),
        deps_type=ReviewerDeps,
        instructions=instructions,
        tools=[run_tests, write],
    )

    first_runtime = AgentRuntime(store, transcript=transcript, retry_running=True)
    first_deps = ReviewerDeps(Path("."), [], asyncio.Event())
    worker = asyncio.create_task(drive("review-1", agent, first_runtime, first_deps, controls))
    while (await store.read("review-1", "tool:c1")).status != "running":
        await asyncio.sleep(0)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker
    print("  ... process A died with c1 running\n")

    second_runtime = AgentRuntime(store, transcript=transcript, retry_running=True)
    second_deps = ReviewerDeps(Path("."), first_deps.touched)
    await drive("review-1", agent, second_runtime, second_deps, controls)


if __name__ == "__main__":
    asyncio.run(demo())
