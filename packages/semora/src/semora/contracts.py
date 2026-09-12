"""Control-flow contracts shared across the effect boundary.

Messages and tool calls are Pydantic AI's. What is ours is the vocabulary of signals that stop an
attempt without being a tool failure, and the shape of an input waiting to enter model context.
"""

from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from pydantic_ai.messages import ModelRequestPart, ToolCallPart

__all__ = [
    "AgentSuspended",
    "ConfirmedEffect",
    "ControlSignal",
    "PendingInput",
    "RetryEffect",
    "StopReason",
    "Suspended",
    "ToolCall",
    "UnresolvedEffect",
]

ToolCall = ToolCallPart
"""One model-issued tool call. `tool_call_id` is its run-scoped durable step name."""

StopReason = Literal["completed", "aborted", "tool", "policy", "suspended"]
"""Why a run ended. `policy` is a `Halt` from a control point; `suspended` means it parked."""


class ControlSignal(Exception):
    """Signal that stops an attempt without becoming a model-visible tool failure."""


class Suspended(ControlSignal):
    """Base of every parking signal."""

    def __init__(self, signal: str) -> None:
        """Name the signal the attempt is waiting on."""
        super().__init__(signal)
        self.signal = signal


class AgentSuspended(Suspended):
    """The attempt parked: a gate asked a person, and nothing waits on the answer in-process.

    Attributes:
        pending_id: External id of the first undecided request, in model order.
        tool_call_id: The call it parks.
        pending: Every undecided `(pending_id, tool_call_id)` of the round, in model order.
    """

    def __init__(
        self, pending_id: str, tool_call_id: str, *, pending: list[tuple[str, str]] | None = None
    ) -> None:
        """Carry the undecided requests a host must route answers to."""
        super().__init__(pending_id)
        self.pending_id = pending_id
        self.tool_call_id = tool_call_id
        self.pending = list(pending) if pending else [(pending_id, tool_call_id)]


class PendingInput(NamedTuple):
    """One queued request part plus the provenance a bare part cannot preserve."""

    kind: str
    part: ModelRequestPart
    origin_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmedEffect:
    """Provider-backed evidence that an indeterminate tool effect completed."""

    decision_id: str
    expected_version: int
    reason: str
    result: Any
    provider_key: str | None = None


@dataclass(frozen=True, slots=True)
class RetryEffect:
    """Provider-backed evidence that retrying an indeterminate tool effect is safe."""

    decision_id: str
    expected_version: int
    reason: str
    provider_key: str | None = None


class UnresolvedEffect(NamedTuple):
    """One model call whose started effect still needs external reconciliation."""

    call_id: str
    tool_name: str
    args: dict[str, Any]
    version: int
