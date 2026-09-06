"""Run a native Pydantic AI agent under a durable permission boundary.

    uv run python examples/permissions.py

The permission class belongs to the Pydantic tool definition. Semora receives that definition at
the execution boundary and durably parks only the write-class call.
"""

import asyncio
from typing import Any

from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolSelector, matches_tool_selector
from semora import (
    AgentRuntime,
    AgentSuspended,
    Continue,
    ControlPlane,
    Ctx,
    MemorySteps,
    Permissions,
    Suspend,
)
from semora.controls import PreToolUse, ToolDecision


def allow(selector: ToolSelector[Any]) -> PreToolUse:
    """Run tools selected by Pydantic AI and park every other call."""

    async def stage(ctx: Ctx, call: ToolCallPart) -> ToolDecision:
        matched = (
            ctx.tool is not None
            and ctx.run is not None
            and await matches_tool_selector(selector, ctx.run, ctx.tool)
        )
        if matched:
            return Continue()
        return Suspend(
            {
                "pending_id": f"approve-{call.tool_call_id}",
                "tool": call.tool_name,
                "args": call.args_as_dict(),
            }
        )

    return stage


def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart("read", {"path": "a.py"}, tool_call_id="c1"),
                ToolCallPart("grep", {"pattern": "TODO"}, tool_call_id="c2"),
                ToolCallPart("write", {"path": "a.py", "text": "x"}, tool_call_id="c3"),
            ]
        )
    return ModelResponse(parts=[TextPart("done")])


async def demo() -> None:
    touched: list[str] = []

    async def read(path: str) -> str:
        """Read a file."""
        touched.append(f"read {path}")
        return f"contents of {path}"

    async def grep(pattern: str) -> str:
        """Search the tree."""
        touched.append(f"grep {pattern}")
        return "3 hits"

    async def write(path: str, text: str) -> str:
        """Write a file."""
        touched.append(f"write {path}")
        return f"wrote {path}"

    agent = Agent(
        FunctionModel(scripted),
        tools=[
            Tool(read, metadata={"permission": "read"}),
            Tool(grep, metadata={"permission": "read"}),
            Tool(write, metadata={"permission": "write"}),
        ],
    )
    runtime = AgentRuntime(MemorySteps())
    controls = ControlPlane(pre_tool_use=Permissions(allow({"permission": "read"})))

    suspension: AgentSuspended | None = None
    try:
        await runtime.run("perm-1", agent, "look around, then fix it", controls=controls)
    except AgentSuspended as error:
        suspension = error
    if suspension is None:
        raise AssertionError("the write must park")

    assert suspension.pending == [("approve-c3", "c3")], suspension.pending
    assert touched == ["read a.py", "grep TODO"], touched
    print(f"reads ran without asking: {touched}")
    print(f"parked on:                {suspension.pending_id}")

    outcome = await runtime.resume(
        "perm-1",
        suspension.pending_id or "",
        {"type": "approve"},
        agent,
        controls=controls,
    )
    assert not outcome.suspended
    assert touched == ["read a.py", "grep TODO", "write a.py"], touched
    print(f"after one approval:       {touched}")


if __name__ == "__main__":
    asyncio.run(demo())
