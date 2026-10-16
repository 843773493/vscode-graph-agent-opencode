"""Agent step 的消息事件发布与 canonical item sink 绑定。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.core.execution_admission_context import require_current_execution_admission
from app.core.phase_observer import observe_phase
from app.domain.itemized.records import CanonicalItemRecord
from app.services.orchestration.event_stream.contracts import CanonicalItemObserverError


class StepEventPublisher:
    """为一个 step 固定 job/agent 身份，防止事件串流。"""

    def __init__(self, bus: Any, *, job_id: str, agent_id: str) -> None:
        self._bus = bus
        self._job_id = job_id
        self._agent_id = agent_id

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._bus.publish(
            job_id=self._job_id,
            event_type=event_type,
            payload=payload,
            agent_id=self._agent_id,
        )


class CanonicalItemSink:
    """把实时 canonical item draft 交给 Saver-owned v2 append API。"""

    def __init__(
        self,
        checkpointer: object,
        *,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        committed_item_observer: Callable[[str, str, Sequence[CanonicalItemRecord]], None] | None = None,
    ) -> None:
        append_items = getattr(checkpointer, "append_items", None)
        if not callable(append_items):
            raise TypeError("canonical item sink 必须绑定 Saver append_items 端口")
        self._append_items = append_items
        complete_tool = getattr(checkpointer, "commit_tool_result", None)
        if not callable(complete_tool):
            raise TypeError("canonical item sink 必须绑定 Saver commit_tool_result 端口")
        self._complete_tool = complete_tool
        self._session_id = session_id
        self._thread_id = thread_id
        self._checkpoint_ns = checkpoint_ns
        self._committed_item_observer = committed_item_observer

    async def append(self, items: Sequence[object]) -> None:
        typed_items = tuple(
            item for item in items if isinstance(item, CanonicalItemRecord)
        )
        if len(typed_items) != len(items):
            raise TypeError("canonical item sink 收到非法 item")
        for item in typed_items:
            if item.semantic_kind == "tool_result" and item.tool_call_id is not None:
                await observe_phase(item.tool_call_id, "canonical.tool_result.before_commit")
        await asyncio.to_thread(
            self._append_items,
            self._session_id,
            typed_items,
            thread_id=self._thread_id,
            checkpoint_ns=self._checkpoint_ns,
        )
        if self._committed_item_observer is not None:
            try:
                self._committed_item_observer(self._session_id, self._thread_id, typed_items)
            except Exception as error:
                raise CanonicalItemObserverError(
                    error, committed_item_ids=tuple(item.item_id for item in typed_items),
                ) from error

    async def complete_tool_result(self, completion: ToolResultCompletion) -> CanonicalItemRecord:
        """实时完成只提交原 admission 的通知；payload 创建权归 Saver。"""
        admission = require_current_execution_admission(langgraph_thread_id=self._thread_id)
        if (admission.session_id, admission.thread_id) != (self._session_id, self._thread_id):
            raise RuntimeError("工具终态 sink 与原 admission owner 不一致")
        await observe_phase(completion.tool_call_id, "canonical.tool_result.before_commit")
        item = await asyncio.to_thread(
            self._complete_tool, session_id=self._session_id, thread_id=self._thread_id,
            job_id=admission.job_id, completion=completion, checkpoint_ns=self._checkpoint_ns,
        )
        if self._committed_item_observer is not None:
            try:
                self._committed_item_observer(self._session_id, self._thread_id, (item,))
            except Exception as error:
                raise CanonicalItemObserverError(error, committed_item_ids=(item.item_id,)) from error
        return item


__all__ = ["CanonicalItemSink", "StepEventPublisher"]
