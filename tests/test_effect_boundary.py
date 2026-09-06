"""Recovery must retain runtime signals and the model-visible policy projection."""

from typing import Any

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition, matches_tool_selector
from semora import AgentRuntime, Continue, ControlPlane, ControlSignal, Ctx, Halt, MemorySteps


def history() -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart("read")]),
        ModelResponse(parts=[ToolCallPart("read", {}, tool_call_id="c1")]),
    ]


def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("done")])


async def test_runtime_signal_leaves_an_unreported_effect() -> None:
    store = MemorySteps()
    signal = ControlSignal("worker stopped")

    async def read() -> str:
        raise signal

    agent = Agent(FunctionModel(answer), tools=[read])
    with pytest.raises(ControlSignal) as raised:
        await AgentRuntime(store).recover("signal", agent, history())

    assert raised.value is signal
    assert (await store.read("signal", "tool:c1")).status == "running"


@pytest.mark.parametrize("keep_controls", [True, False])
async def test_recovery_replays_redacted_result_without_repeating_journal(
    keep_controls: bool,
) -> None:
    store = MemorySteps()
    called: list[str] = []
    journaled: list[str] = []

    async def read() -> dict[str, str]:
        called.append("read")
        return {"text": "123-45-6789"}

    async def redact(ctx: Ctx, call: ToolCallPart, result: Any) -> None:
        journaled.append(call.tool_call_id)
        result["text"] = "***"

    controls = ControlPlane(post_tool_use=redact)
    agent = Agent(FunctionModel(answer), tools=[read])
    runtime = AgentRuntime(store)
    first = await runtime.recover("redaction", agent, history(), controls=controls)
    resumed = await runtime.recover(
        "redaction", agent, history(), controls=controls if keep_controls else None
    )

    for outcome in (first, resumed):
        results = [
            part.content
            for message in outcome.all_messages()
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        assert results == [{"text": "***"}]
    assert called == ["read"]
    assert journaled == ["c1"]
    assert (await store.read("redaction", "tool:c1")).value == {
        "ok": True,
        "value": {"text": "123-45-6789"},
    }


async def test_the_tool_control_points_see_the_tool_definition() -> None:
    """A permission class lives on the tool, not on the call: the gate must be able to read it."""
    seen: dict[str, ToolDefinition | None] = {}

    async def read() -> str:
        return "ok"

    async def gate(ctx: Ctx, call: ToolCallPart) -> Continue:
        seen["pre_tool_use"] = ctx.tool
        return Continue()

    async def journal(ctx: Ctx, call: ToolCallPart, result: Any) -> None:
        seen["post_tool_use"] = ctx.tool

    async def finish(ctx: Ctx, reason: str) -> Halt:
        seen["before_finish"] = ctx.tool
        return Halt("completed")

    controls = ControlPlane(pre_tool_use=gate, post_tool_use=journal, before_finish=finish)
    agent = Agent(FunctionModel(answer), tools=[Tool(read, metadata={"permission": "read"})])
    await AgentRuntime(MemorySteps()).recover("defs", agent, history(), controls=controls)

    for point in ("pre_tool_use", "post_tool_use"):
        definition = seen[point]
        assert definition is not None, point
        assert definition.name == "read"
        assert (definition.metadata or {})["permission"] == "read"
    assert seen["before_finish"] is None, "no tool is being decided at a turn-level point"


async def test_a_gate_selects_tools_with_pydantic_ais_own_selector() -> None:
    """`Ctx.run` is the native context, so a policy selects with `matches_tool_selector`."""
    matched: list[bool] = []

    async def read() -> str:
        return "ok"

    async def gate(ctx: Ctx, call: ToolCallPart) -> Continue:
        assert ctx.run is not None and ctx.tool is not None
        matched.append(await matches_tool_selector({"permission": "read"}, ctx.run, ctx.tool))
        return Continue()

    agent = Agent(FunctionModel(answer), tools=[Tool(read, metadata={"permission": "read"})])
    await AgentRuntime(MemorySteps()).recover(
        "selector", agent, history(), controls=ControlPlane(pre_tool_use=gate)
    )

    assert matched == [True]
