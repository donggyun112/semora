"""Semora composes with Pydantic AI's public durability capabilities."""

from collections.abc import Awaitable, Callable

import pytest
from pydantic_ai import Agent
from pydantic_ai.durable_exec import (
    JSON_CODEC,
    BaseDurabilityCapability,
    DurabilityEngineSpec,
    DurableOperationId,
    JournalCallableOperationBackend,
    ModelRequestId,
    RoleBasedOperationConfig,
    ToolsetCallToolId,
)
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
from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence
from semora import AgentRuntime, ControlSignal, Indeterminate, MemorySteps
from semora.journal import step_key


class Files:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def write(self, path: str) -> str:
        self.ran.append(path)
        return f"wrote {path}"


def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returned = [
        part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    if returned:
        return ModelResponse(parts=[TextPart("done")])
    return ModelResponse(parts=[ToolCallPart("write", {"path": "memo.txt"}, tool_call_id="c1")])


def pending_history() -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart("write")]),
        ModelResponse(parts=[ToolCallPart("write", {"path": "memo.txt"}, tool_call_id="c1")]),
    ]


class ImmediateBackend(JournalCallableOperationBackend[None]):
    def __init__(self, operations: list[DurableOperationId]) -> None:
        super().__init__(
            agent_name="semora-composition",
            config=RoleBasedOperationConfig(
                model=None,
                event=None,
                capability=None,
                tool=None,
            ),
        )
        self.operations = operations

    async def execute(
        self,
        *,
        operation_id: DurableOperationId,
        name: str,
        body: Callable[[], Awaitable[object]],
        cache_key: tuple[object, ...],
        config: None,
    ) -> object:
        self.operations.append(operation_id)
        assert name
        _ = cache_key, config
        return await body()


class ImmediateDurability(BaseDurabilityCapability[None]):
    engine_spec = DurabilityEngineSpec(
        engine_name="test",
        durable_unit_noun="operation",
        durable_container_noun="test run",
        codec=JSON_CODEC,
        wrapped_toolset_kinds=frozenset({"function"}),
        toolset_lifecycles={"function": "enter-always"},
    )

    def __init__(
        self,
        operations: list[DurableOperationId],
        model: FunctionModel,
    ) -> None:
        super().__init__(models={"script": model}, name="semora-composition")
        self.backend = ImmediateBackend(operations)

    @property
    def in_durable_context(self) -> bool:
        return True

    def get_durable_operation_backend(self) -> ImmediateBackend:
        return self.backend


def agent_and_files() -> tuple[Agent[None, str], Files, FunctionModel]:
    files = Files()
    model = FunctionModel(script)
    agent = Agent(model, name="semora-composition")
    agent.tool_plain(files.write)
    return agent, files, model


async def test_runtime_composes_with_a_public_pydantic_durability_backend() -> None:
    store = MemorySteps()
    operations: list[DurableOperationId] = []
    agent, files, model = agent_and_files()

    outcome = await AgentRuntime(store).run(
        "compose", agent, "write", capabilities=[ImmediateDurability(operations, model)]
    )

    assert outcome.output == "done"
    assert files.ran == ["memo.txt"]
    assert any(isinstance(operation, ModelRequestId) for operation in operations)
    assert any(isinstance(operation, ToolsetCallToolId) for operation in operations)
    assert (await store.read("compose", "tool:c1")).status == "done"


async def test_semora_replays_a_committed_effect_with_durability_attached() -> None:
    store = MemorySteps()
    operations: list[DurableOperationId] = []
    agent, files, model = agent_and_files()
    runtime = AgentRuntime(store)
    durability = ImmediateDurability(operations, model)

    first = await runtime.recover(
        "compose-replay", agent, pending_history(), capabilities=[durability]
    )
    second = await runtime.recover(
        "compose-replay",
        agent,
        pending_history(),
        capabilities=[ImmediateDurability(operations, model)],
    )

    assert first.output == second.output == "done"
    assert files.ran == ["memo.txt"]


async def test_durability_does_not_convert_a_semora_runtime_signal() -> None:
    store = MemorySteps()
    operations: list[DurableOperationId] = []
    signal = ControlSignal("worker stopped")

    async def write(path: str) -> str:
        raise signal

    model = FunctionModel(script)
    agent = Agent(model, name="semora-signal")
    agent.tool_plain(write)

    with pytest.raises(ControlSignal) as raised:
        await AgentRuntime(store).recover(
            "compose-signal",
            agent,
            pending_history(),
            capabilities=[ImmediateDurability(operations, model)],
        )

    assert raised.value is signal
    assert (await store.read("compose-signal", step_key("c1"))).status == "running"


async def test_running_effect_stays_indeterminate_with_durability_attached() -> None:
    store = MemorySteps()
    owner = "interrupted-worker"
    token = await store.acquire("compose-running", owner, 30)
    assert token
    assert await store.start("compose-running", step_key("c1"), token)
    await store.release("compose-running", owner)
    operations: list[DurableOperationId] = []
    agent, files, model = agent_and_files()

    with pytest.raises(Indeterminate):
        await AgentRuntime(store).recover(
            "compose-running",
            agent,
            pending_history(),
            capabilities=[ImmediateDurability(operations, model)],
        )

    assert files.ran == []


async def test_runtime_composes_with_step_persistence() -> None:
    semora_store = MemorySteps()
    step_store = InMemoryStepStore()
    capability = StepPersistence(store=step_store, agent_name="semora-test")
    agent, files, _ = agent_and_files()

    outcome = await AgentRuntime(semora_store).run(
        "branch-1",
        agent,
        "write",
        conversation_id="conversation-1",
        capabilities=[capability],
    )

    runs = await step_store.list_runs(conversation_id="conversation-1")
    assert outcome.output == "done"
    assert files.ran == ["memo.txt"]
    assert len(runs) == 1
    assert runs[0].run_id != "branch-1"
    events = await step_store.list_events(run_id=runs[0].run_id)
    assert any(event.kind == "tool_call_completed" for event in events)
