from typing import Any

from pydantic_ai import Agent
from pydantic_ai.agent import WrapperAgent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.run import AgentRunResult

from semora import AgentRuntime


def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("wrapped")])


class RecordingWrapper(WrapperAgent[None, str]):
    def __init__(self, wrapped: Agent[None, str]) -> None:
        super().__init__(wrapped)
        self.called = False

    async def run(self, *args: Any, **kwargs: Any) -> AgentRunResult[Any]:  # type: ignore[override]
        self.called = True
        return await super().run(*args, **kwargs)


async def test_runtime_drives_the_public_abstract_agent_interface() -> None:
    native = Agent(FunctionModel(answer))
    wrapped = RecordingWrapper(native)

    outcome = await AgentRuntime().run("wrapped-run", wrapped, "hello")

    assert outcome.output == "wrapped"
    assert wrapped.called
