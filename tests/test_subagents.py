"""Pydantic Harness delegation composes with Semora's parent execution boundary."""

import asyncio
import contextlib
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness import SubAgent, SubAgents
from semora import AgentRuntime, MemorySteps, MemoryTranscript
from semora.dispatch import Recover


def child_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(parts=[ToolCallPart("read", {"path": "a"}, tool_call_id="child-c1")])
    return ModelResponse(parts=[TextPart("child done")])


def lead_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "delegate_task",
                    {"agent_name": "Explorer", "task": "look around"},
                    tool_call_id="lead-c1",
                )
            ]
        )
    return ModelResponse(parts=[TextPart("lead done")])


def explorer(reads: list[str]) -> Agent[None, str]:
    async def read(path: str) -> str:
        """Read a file."""
        reads.append(path)
        return f"<{path}>"

    return Agent(
        FunctionModel(child_model),
        name="Explorer",
        description="Explores the codebase without modifying anything.",
        tools=[read],
    )


def lead(model: FunctionModel, child: Agent[None, str], *tools: Any) -> Agent[None, str]:
    return Agent(
        model,
        name="Lead",
        description="Coordinates the explorer.",
        tools=tools,
        capabilities=[SubAgents(agents=[SubAgent(child)], agent_folders=None)],
    )


async def test_a_delegation_is_one_durable_parent_effect() -> None:
    ledger = MemorySteps()
    reads: list[str] = []
    agent = lead(FunctionModel(lead_model), explorer(reads))

    outcome = await AgentRuntime(ledger).run("lead-1", agent, "go")

    assert outcome.output == "lead done"
    assert reads == ["a"]
    delegation = await ledger.read("lead-1", "tool:lead-c1")
    assert delegation.status == "done" and delegation.value == {
        "ok": True,
        "value": "child done",
    }


async def test_a_recovered_parent_does_not_delegate_again() -> None:
    ledger, transcript = MemorySteps(), MemoryTranscript()
    reads: list[str] = []
    child = explorer(reads)
    block = asyncio.Event()

    def slow_lead(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "delegate_task",
                        {"agent_name": "Explorer", "task": "look around"},
                        tool_call_id="lead-c1",
                    ),
                    ToolCallPart("wait", {}, tool_call_id="lead-c2"),
                ]
            )
        return ModelResponse(parts=[TextPart("lead done")])

    async def wait() -> str:
        """Block until the test lets go."""
        await block.wait()
        return "waited"

    agent = lead(FunctionModel(slow_lead), child, wait)
    runtime = AgentRuntime(ledger, transcript=transcript)
    worker = asyncio.create_task(runtime.run("lead-2", agent, "go"))
    while (await ledger.read("lead-2", "tool:lead-c2")).status != "running":
        await asyncio.sleep(0)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker
    block.set()

    later = AgentRuntime(ledger, transcript=transcript, retry_running=True)
    outcome = await later.dispatch("lead-2", agent, Recover())

    assert outcome.output == "lead done"
    assert reads == ["a"], "the committed delegation replayed instead of running the child twice"
