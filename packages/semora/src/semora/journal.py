"""Durable model and tool effect transitions used by the execution boundary."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, cast

from pydantic import TypeAdapter
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_core import to_jsonable_python
from semora_store import Contended, ExecutionStore, Fenced, Indeterminate

from .contracts import ControlSignal
from .transcript import stripped

__all__ = ["EffectJournal", "after_key", "model_step_key", "step_key"]

_NOTHING_HAPPENED = (ApprovalRequired, CallDeferred, ModelRetry)
"""Signals a tool raises before any side effect. Intent is cleared so the call can rerun."""

_RUNTIME_SIGNALS = (ControlSignal, Indeterminate, Fenced, Contended)
"""Ledger signals. Never convert these signals into tool results."""

_RESPONSE = TypeAdapter(ModelResponse)


def step_key(call_id: str) -> str:
    """Name the durable step for one tool call."""
    return f"tool:{call_id}"


def after_key(call_id: str) -> str:
    """Name the marker showing that post-tool policy ran for one call."""
    return f"after:{call_id}"


def model_step_key(request: ModelRequestContext) -> str:
    """Derive one model effect ID from its model, tools, and visible context."""
    model = request.model_id or getattr(request.model, "model_id", None) or repr(request.model)
    body = {
        "model": model,
        "tools": [
            [tool.name, tool.description, tool.parameters_json_schema]
            for tool in request.model_request_parameters.function_tools
        ],
        "messages": stripped(to_jsonable_python(request.messages)),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"agent:model:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


class EffectJournal:
    """Commit and replay model and tool effects for one fenced attempt."""

    def __init__(
        self,
        store: ExecutionStore | None,
        branch_id: str,
        token: int,
        *,
        retry_running: bool,
    ) -> None:
        """Bind storage, branch identity, fencing token, and retry policy."""
        self.store = store
        self.branch_id = branch_id
        self.token = token
        self.retry_running = retry_running

    async def recorded(self, call_id: str) -> bool:
        """Whether this branch already holds the call's completed effect."""
        if self.store is None:
            return False
        return (await self.store.read(self.branch_id, step_key(call_id))).status == "done"

    async def model(
        self,
        request: ModelRequestContext,
        handler: Callable[[ModelRequestContext], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Execute or replay one model request as a durable step."""
        if self.store is None:
            return await handler(request)
        key = model_step_key(request)
        step = await self.store.read(self.branch_id, key)
        if step.status == "done":
            return _RESPONSE.validate_python(step.value["response"])
        if step.status == "running":
            if not self.retry_running:
                raise Indeterminate(self.branch_id, key)
            await self.store.forget(self.branch_id, key, self.token)
        if not await self.store.start(self.branch_id, key, self.token):
            raise Indeterminate(self.branch_id, key)
        try:
            response = await handler(request)
        except asyncio.CancelledError:
            raise
        except BaseException:
            await self.store.forget(self.branch_id, key, self.token)
            raise
        await self.store.finish_effect(
            self.branch_id,
            key,
            {"type": "model_result", "response": to_jsonable_python(response)},
            self.token,
        )
        return response

    async def tool(
        self,
        call_id: str,
        args: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> dict[str, Any]:
        """Replay, reject ambiguity, or execute and commit one tool effect."""
        if self.store is None:
            return await self._execute(None, args, handler)
        key = step_key(call_id)
        step = await self.store.read(self.branch_id, key)
        if step.status == "done":
            return cast(dict[str, Any], step.value)
        if step.status == "running":
            if not self.retry_running:
                raise Indeterminate(self.branch_id, key)
            await self.store.forget(self.branch_id, key, self.token)
        if not await self.store.start(self.branch_id, key, self.token):
            raise Indeterminate(self.branch_id, key)
        return await self._execute(key, args, handler)

    async def _execute(
        self,
        key: str | None,
        args: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> dict[str, Any]:
        try:
            value = await handler(args)
        except _NOTHING_HAPPENED:
            if self.store is not None and key is not None:
                await self.store.forget(self.branch_id, key, self.token)
            raise
        except _RUNTIME_SIGNALS:
            raise
        except Exception as error:
            record: dict[str, Any] = {"ok": False, "error": str(error)}
        else:
            record = {"ok": True, "value": value}
        if self.store is not None and key is not None:
            await self.store.finish_effect(self.branch_id, key, record, self.token)
        return record

    async def project_once(
        self,
        call_id: str,
        record: dict[str, Any],
        project: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> dict[str, Any]:
        """Run post-tool policy once and replay its model-visible projection."""
        key = after_key(call_id)
        if self.store is not None:
            journal = await self.store.read(self.branch_id, key)
            if journal.status == "done":
                projection = journal.value.get("record")
                if not isinstance(projection, dict):
                    raise ValueError("journal completion has no recorded model-visible result")
                return deepcopy(projection)
        if project is None:
            return record
        projected = deepcopy(record)
        await project(projected)
        if self.store is not None:
            await self.store.write_control(
                self.branch_id, key, {"hooked": True, "record": projected}, self.token
            )
        return projected
