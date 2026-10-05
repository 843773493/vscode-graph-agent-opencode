"""Agent step 到 v2 execution/model-call/context owner 的适配器。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from langchain_core.messages import AIMessage

from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.enums import CanonicalItemStatus, SemanticKind
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)


class StepModelCallAdapter:
    """把实时模型 callback 绑定到 Saver-owned v2 context/execution。"""

    def __init__(
        self,
        *,
        checkpointer: object,
        session_id: str,
        thread_id: str,
        job_id: str,
        checkpoint_ns: str,
    ) -> None:
        # 生产 step 只接受完整的 v2 Saver；在打开消息流前验证必需端口。
        self._ports: dict[str, Callable[..., object]] = {}
        for name in (
            "register_model_call",
            "consume_prepared_context_for_dispatch",
            "update_model_call_outcome",
            "converge_execution",
        ):
            port = getattr(checkpointer, name, None)
            if not callable(port):
                raise TypeError(f"v2 checkpoint saver 缺少可调用端口: {name}")
            self._ports[name] = port
        self._session_id = session_id
        self._thread_id = thread_id
        self._job_id = job_id
        self._checkpoint_ns = checkpoint_ns
        self._previous_model_call_id: str | None = None
        self._last_assembly_id: str | None = None
        self._last_execution_id: str | None = None
        self._failed_call_retry_binding_available = False
        self._final_convergence_item_id: str | None = None
        self._logger = logging.getLogger(__name__)

    async def register(
        self,
        model_call_id: str,
        attempt: int,
        model: str,
    ) -> str:
        """只绑定 dispatch 前已封存的 assembly，不从事件重建请求事实。"""
        register = self._ports["register_model_call"]
        consume_prepared = self._ports["consume_prepared_context_for_dispatch"]
        admission = require_current_execution_admission(
            langgraph_thread_id=self._thread_id
        )
        self._validate_admission(admission)
        prepared = await asyncio.to_thread(
            consume_prepared,
            self._session_id,
            self._thread_id,
            execution_id=admission.execution_id,
            checkpoint_ns=self._checkpoint_ns,
        )
        if prepared is None:
            if (
                not self._failed_call_retry_binding_available
                or self._last_assembly_id is None
                or self._last_execution_id is None
                or self._previous_model_call_id is None
            ):
                raise RuntimeError(
                    "model-call 缺少 dispatch 前已封存的 prepared context: "
                    f"session_id={self._session_id} job_id={self._job_id} "
                    f"model_call_id={model_call_id}"
                )
            # Provider 在一次已封存 dispatch 内部自动切换 fallback 时不会再次经过
            # context middleware。只有前一调用已提交 failed 终态时，才允许新 attempt
            # 复用同一份不可变 assembly/execution binding。
            assembly_id = self._last_assembly_id
            execution_id = self._last_execution_id
        else:
            if not isinstance(prepared, dict):
                raise TypeError("prepared context dispatch handle 必须是 object")
            assembly_id = prepared.get("assembly_id")
            execution_id = prepared.get("execution_id")
            if (
                not isinstance(assembly_id, str)
                or not assembly_id.strip()
                or not isinstance(execution_id, str)
                or not execution_id.strip()
            ):
                raise RuntimeError(
                    "prepared context dispatch handle 缺少 assembly_id/execution_id"
                )
        if admission.execution_id != execution_id:
            raise RuntimeError(
                "prepared context dispatch 与当前 execution admission 不一致: "
                f"session_id={self._session_id}, thread_id={self._thread_id}, "
                f"turn_id={admission.turn_id}, execution_id={execution_id}"
            )
        await asyncio.to_thread(
            register,
            self._session_id,
            thread_id=self._thread_id,
            execution_id=execution_id,
            model_call_id=model_call_id,
            attempt=attempt,
            provider=model,
            retry_of_model_call_id=self._previous_model_call_id,
            assembly_id=assembly_id,
            dispatch_state="dispatched",
            checkpoint_ns=self._checkpoint_ns,
        )
        self._last_assembly_id = assembly_id
        self._last_execution_id = execution_id
        self._failed_call_retry_binding_available = False
        self._logger.warning(
            "[itemized-context] model-call registration committed: "
            "model_call_id=%s execution_id=%s assembly_id=%s",
            model_call_id,
            execution_id,
            assembly_id,
        )
        self._previous_model_call_id = model_call_id
        return execution_id

    def converge_final_message(self, message: AIMessage) -> None:
        """在最终 checkpoint message 落盘前提交同一份 terminal item。"""
        admission = require_current_execution_admission(
            langgraph_thread_id=self._thread_id
        )
        self._validate_admission(admission)
        codec = LangChainMessageCodec()
        message_id = message.id
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("最终 assistant message 缺少稳定 message_id")
        items = codec.items_for_message(
            message,
            item_sequence=1,
            message_id=message_id,
            turn_id=admission.turn_id,
            execution_id=admission.execution_id,
            timestamp=datetime.now(UTC).isoformat(),
        )
        if len(items) != 1:
            raise ValueError("最终 assistant message 不得包含 tool-call content group")
        item = items[0]
        if (
            item.item_id != f"item-{message_id}"
            or item.semantic_kind != SemanticKind.ASSISTANT_OUTPUT
            or item.status != CanonicalItemStatus.COMPLETED.value
            or item.turn_id != admission.turn_id
            or item.turn_scope != ("turn_member" if admission.turn_id is not None else None)
        ):
            raise ValueError("最终 assistant message 未生成 admission 归属的 completed item")
        converge = self._ports["converge_execution"]
        execution_id = admission.execution_id
        converge(
            self._session_id,
            turn_id=admission.turn_id,
            thread_id=self._thread_id,
            execution_id=execution_id,
            outcome="completed",
            turn_status="completed" if admission.turn_id is not None else None,
            items=(item,),
            final_item_id=item.item_id if admission.turn_id is not None else None,
            assembly_id=self._last_assembly_id,
            checkpoint_ns=self._checkpoint_ns,
        )
        self._final_convergence_item_id = item.item_id

    async def converge_final_checkpoint(self, final_message_id: str | None) -> None:
        """确认 checkpoint 与 checkpoint 写入前的终态提交一致。"""
        converge = self._ports["converge_execution"]
        admission = require_current_execution_admission(
            langgraph_thread_id=self._thread_id
        )
        self._validate_admission(admission)
        if final_message_id is not None:
            if not final_message_id:
                raise ValueError("最终 checkpoint 的 message_id 不得为空")
            if self._final_convergence_item_id != f"item-{final_message_id}":
                raise RuntimeError(
                    "最终 assistant item 必须在 checkpoint 写入前与 execution 原子收敛: "
                    f"execution_id={admission.execution_id} message_id={final_message_id}"
                )
            return
        convergence_kwargs: dict[str, object] = {
            "turn_id": admission.turn_id,
            "thread_id": self._thread_id,
            "execution_id": admission.execution_id,
            "outcome": "completed_empty",
            "turn_status": "completed_empty" if admission.turn_id is not None else None,
            "final_item_id": None,
            "checkpoint_ns": self._checkpoint_ns,
        }
        if self._last_assembly_id is not None:
            convergence_kwargs["assembly_id"] = self._last_assembly_id
        await asyncio.to_thread(converge, self._session_id, **convergence_kwargs)

    def _validate_admission(self, admission: ExecutionAdmission) -> None:
        if (
            admission.session_id != self._session_id
            or admission.thread_id != self._thread_id
            or admission.job_id != self._job_id
            or admission.execution_origin
            not in {"user_turn", "internal"}
            or (admission.execution_origin == "user_turn")
            != (admission.turn_id is not None)
        ):
            raise RuntimeError(
                "当前 model-call 与 execution admission 不一致: "
                f"session_id={self._session_id}, thread_id={self._thread_id}, "
                f"job_id={self._job_id}, execution_id={admission.execution_id}"
            )

    async def update_outcome(self, model_call_id: str, outcome: str) -> None:
        """把 provider/model stream outcome 写入同一个 v2 model-call owner。"""
        update = self._ports["update_model_call_outcome"]
        dispatch_state = (
            "completed"
            if outcome in {"completed", "completed_empty"}
            else "failed"
            if outcome in {"failed", "cancelled", "interrupted"}
            else "unknown"
        )
        await asyncio.to_thread(
            update,
            self._session_id,
            model_call_id=model_call_id,
            thread_id=self._thread_id,
            outcome=outcome,
            dispatch_state=dispatch_state,
            checkpoint_ns=self._checkpoint_ns,
        )
        if model_call_id == self._previous_model_call_id:
            self._failed_call_retry_binding_available = outcome == "failed"


__all__ = ["StepModelCallAdapter"]
