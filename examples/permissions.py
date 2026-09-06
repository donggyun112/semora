"""A permission policy by tool class: read-class tools run, everything else asks a person.

    uv run python examples/permissions.py

The permission class belongs to the tool, so the host declares it once — `@tool(metadata=...)`
here, `Tool(fn, metadata=...)` for an implementation written elsewhere — and the gate reads it
from `ctx.tool`. A call carries a name and arguments and nothing else, so a policy that matched
on `call.tool_name` would be a second list to keep in step with the first.

Selection is Pydantic AI's `matches_tool_selector` over `ctx.run`, not a rule of our own: the
same `ToolSelector` a capability or an agent spec uses picks the tools a permission covers.

A tool declared `@tool(requires_approval=True)` is a floor this policy cannot lower: read-class
tools must not carry that declaration.
"""

import asyncio
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolSelector, matches_tool_selector
from semora import Agent, Continue, Ctx, MemorySteps, Permissions, Suspend, tool
from semora.controls import PreToolUse, ToolDecision


def allow(selector: ToolSelector[Any]) -> PreToolUse:
    """Run the tools this selector matches; park every other call, one `pending_id` each.

    The selector is Pydantic AI's own: `'all'`, a sequence of names, a metadata mapping matched by
    deep inclusion, or a predicate. Matching is `matches_tool_selector`, so a policy and an agent
    spec select tools by the same rule.
    """

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


class Worker(Agent):
    """Reads freely; a write waits for a person."""

    llm = FunctionModel(scripted)
    store = MemorySteps()
    pre_tool_use = Permissions(allow({"permission": "read"}))  # the whole policy, one attribute

    def __init__(self, **kwargs: Any) -> None:
        self.touched: list[str] = []
        super().__init__(**kwargs)

    @tool(metadata={"permission": "read"})
    async def read(self, path: str) -> str:
        """Read a file."""
        self.touched.append(f"read {path}")
        return f"contents of {path}"

    @tool(metadata={"permission": "read"})
    async def grep(self, pattern: str) -> str:
        """Search the tree."""
        self.touched.append(f"grep {pattern}")
        return "3 hits"

    @tool(metadata={"permission": "write"})
    async def write(self, path: str, text: str) -> str:
        """Write a file. An effect."""
        self.touched.append(f"write {path}")
        return f"wrote {path}"


async def demo() -> None:
    agent = Worker(branch_id="perm-1")

    parked = await agent.run("look around, then fix it")
    assert parked.suspended, "the write must park"
    assert parked.pending == (("approve-c3", "c3"),), parked.pending
    assert agent.touched == ["read a.py", "grep TODO"], agent.touched
    print(f"reads ran without asking: {agent.touched}")
    print(f"parked on:                {parked.pending_id}")

    outcome = await agent.resume({"type": "approve"})
    assert not outcome.suspended
    assert agent.touched == ["read a.py", "grep TODO", "write a.py"], agent.touched
    print(f"after one approval:       {agent.touched}")


if __name__ == "__main__":
    asyncio.run(demo())
