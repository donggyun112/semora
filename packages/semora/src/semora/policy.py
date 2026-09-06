"""Tool-policy decisions used by the Pydantic execution boundary."""

from collections.abc import Collection, Mapping
from dataclasses import replace
from typing import Any, NamedTuple

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.tools import ToolDefinition

from .controls import Continue, Controls, Ctx, Deny, ResumeInput, Suspend, ToolDecision

__all__ = ["CONCURRENCY_SAFE", "NOT_EXECUTED", "PolicyRunner", "Resumed", "last_text"]

CONCURRENCY_SAFE = "concurrency_safe"
"""Tool metadata flag declaring that call order cannot affect the batch."""

NOT_EXECUTED = (
    "not executed: an earlier call in this round awaits approval; reissue it once that is decided"
)
"""Model-visible result for a call held behind a suspended call in the same batch."""


class Resumed(NamedTuple):
    """The answer, request, and old rules for a call returning from suspension."""

    answer: dict[str, Any]
    request: dict[str, Any]
    rules_version: str


def last_text(messages: list[ModelMessage]) -> str:
    """Return the latest assistant text visible to policy."""
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return "".join(part.content for part in message.parts if isinstance(part, TextPart))
    return ""


class PolicyRunner:
    """Evaluate tool controls and retain policy state for one agent attempt."""

    def __init__(
        self,
        *,
        controls: Controls | None,
        rules_version: str,
        subject: str,
        resumed: Mapping[str, Resumed],
        regate: Collection[str],
    ) -> None:
        """Bind controls and the resume state used for this attempt."""
        self.controls = controls
        self.rules_version = rules_version
        self.subject = subject
        self.resumed = resumed
        self.regate = set(regate)
        self.calls_made: list[dict[str, Any]] = []
        self._approval_tools: set[str] = set()
        self._suspended_this_round = False

    def prepare_tools(self, tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        """Normalize approval tools and serialize order-sensitive tool batches."""
        prepared: list[ToolDefinition] = []
        for tool in tool_defs:
            if tool.kind == "unapproved":
                self._approval_tools.add(tool.name)
                tool = replace(tool, kind="function")
            if not (tool.metadata or {}).get(CONCURRENCY_SAFE):
                tool = replace(tool, sequential=True)
            prepared.append(tool)
        return prepared

    def begin_tool_round(self) -> None:
        """Reset the batch-local suspension marker."""
        self._suspended_this_round = False

    def context(
        self,
        ctx: RunContext[Any],
        *,
        pending: list[ModelMessage] | None = None,
        tool: ToolDefinition | None = None,
    ) -> Ctx:
        """Build the stable Semora control context from Pydantic's run context."""
        messages = [*ctx.messages, *(pending or [])]
        return Ctx(
            turn=ctx.run_step,
            messages=messages,
            calls_made=list(self.calls_made),
            text=last_text(messages),
            subject=self.subject,
            tool=tool,
            run=ctx,
        )

    async def decide_tool(
        self,
        here: Ctx,
        call: ToolCallPart,
        args: Any,
        *,
        approved: bool,
        recorded: bool,
    ) -> ToolDecision:
        """Decide a fresh, resumed, or already-recorded tool call."""
        effective_call = call
        if call.tool_call_id not in self.regate and recorded:
            decision: ToolDecision = Continue()
        elif approved:
            if isinstance(args, dict):
                effective_call = replace(call, args=args)
            decision = await self._resume_decision(here, effective_call)
        else:
            decision = await self._gate(here, call)

        if isinstance(decision, Suspend):
            if not decision.request.get("pending_id"):
                raise ValueError(f"suspension of {call.tool_call_id!r} carries no pending_id")
            self._suspended_this_round = True
        self.note_tool(effective_call, refused=not isinstance(decision, Continue))
        return decision

    async def _gate(self, here: Ctx, call: ToolCallPart) -> ToolDecision:
        decision = (
            await self.controls.pre_tool_use(here, call)
            if self.controls is not None
            else Continue()
        )
        if isinstance(decision, Continue) and call.tool_name in self._approval_tools:
            decision = Suspend({"pending_id": call.tool_call_id})
        if self._suspended_this_round and not isinstance(decision, Suspend):
            return Deny(NOT_EXECUTED)
        return decision

    async def _resume_decision(self, here: Ctx, call: ToolCallPart) -> ToolDecision:
        resumed = self.resumed.get(call.tool_call_id)
        if resumed is None or self.controls is None:
            return Continue()
        return await self.controls.on_resume(
            here,
            call,
            ResumeInput(resumed.answer, resumed.request, resumed.rules_version, self.rules_version),
        )

    def note_tool(self, call: ToolCallPart, *, refused: bool) -> None:
        """Append one tool decision to the policy-visible call history."""
        entry: dict[str, Any] = {
            "id": call.tool_call_id,
            "name": call.tool_name,
            "input": call.args_as_dict(),
        }
        if refused:
            entry["refused"] = True
        self.calls_made.append(entry)

    async def after_tool(
        self,
        here: Ctx,
        call: ToolCallPart,
        record: dict[str, Any],
    ) -> None:
        """Run post-tool policy against the committed result's visible projection."""
        if self.controls is None:
            return
        result = record["value"] if record["ok"] else {"type": "error", "message": record["error"]}
        await self.controls.post_tool_use(here, call, result)
