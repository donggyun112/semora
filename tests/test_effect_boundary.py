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
from semora import (
    AgentRuntime,
    Continue,
    ControlPlane,
    ControlSignal,
    Ctx,
    Deny,
    Effects,
    ExecutionBoundary,
    Halt,
    Indeterminate,
    MemorySteps,
    Permissions,
    ResumeInput,
    Suspend,
)
from semora.journal import EffectJournal, step_key
from semora.policy import NOT_EXECUTED, PolicyRunner, Resumed


def history() -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart("read")]),
        ModelResponse(parts=[ToolCallPart("read", {}, tool_call_id="c1")]),
    ]


def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("done")])


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
    projected: list[dict[str, Any]] = []

    async def project(record: dict[str, Any]) -> None:
        projected.append(dict(record))

    committed = {"ok": True, "value": "done"}
    try:
        first = await journal.project_once("c1", committed, project)
        second = await journal.project_once("c1", committed, project)
    finally:
        await store.release("journal-project", "test")

    assert first == second == committed
    assert projected == [committed]


async def test_policy_runner_asks_pre_tool_for_a_fresh_call() -> None:
    async def ask(ctx: Ctx, call: ToolCallPart) -> Suspend:
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

    async def recheck(ctx: Ctx, call: ToolCallPart, resume: ResumeInput) -> Continue:
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
    async def unexpected(ctx: Ctx, call: ToolCallPart) -> Continue:
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
    async def gate(ctx: Ctx, call: ToolCallPart) -> Continue | Suspend:
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


async def test_execution_boundary_is_the_native_agent_capability() -> None:
    """The primary boundary works directly with Pydantic AI; the old name stays compatible."""
    store = MemorySteps()
    called: list[str] = []

    async def read() -> str:
        called.append("read")
        return "ok"

    agent = Agent(FunctionModel(answer), tools=[read])
    result = await Agent.run(
        agent,
        None,
        message_history=history(),
        capabilities=[ExecutionBoundary(store, "native-boundary")],
    )

    assert result.output == "done"
    assert called == ["read"]
    assert (await store.read("native-boundary", "tool:c1")).value["value"] == "ok"
    assert Effects is ExecutionBoundary
