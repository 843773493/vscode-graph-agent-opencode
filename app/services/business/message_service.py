"""消息准入、checkpoint 读取与可见消息查询。"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from app.abstractions.job_executor import JobRuntimeStateProtocol
from app.abstractions.session_context import AgentContextState
from app.core.checkpoint_config import build_checkpoint_config
from app.core.identifier import create_prefixed_id
from app.core.session_thread_context import session_thread_owner_scope
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
    InternalExecutionInterrupt,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import (
    ExecutionQueueInput,
    ExecutionQueueRecord,
    MessageSubmissionConflictError,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import (
    CanonicalItemRecord,
    InternalDisplayProvenance,
    user_input_payload,
)
from app.schemas.internal_v2.common import CursorPage, MessageRole
from app.schemas.internal_v2.message import (
    AgentStateMessagesDTO,
    MessageCreateRequest,
    MessageDTO,
)
from app.schemas.internal_v2.pending_request import PendingRequestControl
from app.services.business.runtime_reminder import (
    build_runtime_reminder_decision,
    submit_runtime_reminder,
)
from app.services.business.session_turn_history.visible_page import visible_message_page
from app.services.business.user_content_builder import (
    UserContentBuilder,
    build_human_response_metadata,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.attachment_content_service import (
    AttachmentContentService,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    acheckpoint_messages,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    canonical_message_anchor,
    message_annotations,
)
from app.services.mapping.agent_state_messages import project_agent_state_records
from app.services.mapping.itemized.message_reasoning_merge import (
    merge_canonical_reasoning,
)
from app.services.mapping.itemized.provider_history import public_message_record
from app.services.mapping.message_content import (
    attachments_from_message_metadata,
    checkpoint_message_to_dto,
    is_user_visible_message,
)


class MessageService:
    def __init__(
        self,
        *,
        main_thread_resolver: Callable[[str], str],
        checkpointer: BaseCheckpointSaver | None = None,
        attachment_store: AttachmentBlobStore | None = None,
        canonical_item_reader: Callable[[str, str], Sequence[CanonicalItemRecord]]
        | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        # 普通 Session 的消息、历史与新建 DTO 只归属权威 main thread；
        # 身份一律由 catalog 冻结指针解析，禁止用 session_id 冒充。
        self._main_thread_resolver = main_thread_resolver
        self._checkpointer = checkpointer
        self._attachment_store = attachment_store
        self._canonical_item_reader = canonical_item_reader
        self._workspace_root = workspace_root

    def _resolve_main_thread_id(self, session_id: str) -> str:
        """解析会话的权威 main thread id；缺指针时保持 KeyError 语义。"""
        return self._main_thread_resolver(session_id)

    async def load_checkpoint_tuple(
        self,
        session_id: str,
        thread_id: str,
    ) -> CheckpointTuple | None:
        if self._checkpointer is None:
            return None
        owner = MutationIntentOwner(session_id=session_id, thread_id=thread_id)
        with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
            checkpoint = await self._checkpointer.aget_tuple(build_checkpoint_config(thread_id))
            if checkpoint is None:
                return None
            messages = await acheckpoint_messages(self._checkpointer, checkpoint)
            return checkpoint._replace(checkpoint={
                **checkpoint.checkpoint, "channel_values": {
                    **checkpoint.checkpoint["channel_values"], "messages": messages,
                },
            })

    async def list(
        self,
        session_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CursorPage[MessageDTO]:
        thread_id = self._resolve_main_thread_id(session_id)
        checkpoint_tuple = await self.load_checkpoint_tuple(session_id, thread_id)
        if checkpoint_tuple is None:
            return CursorPage(items=[], next_cursor=None, has_more=False)
        return visible_message_page(
            self.visible_messages_from_checkpoint(
                session_id, thread_id, checkpoint_tuple
            ),
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_id=str(checkpoint_tuple.checkpoint.get("id") or ""),
            limit=limit,
            cursor=cursor,
        )

    async def get(self, session_id: str, message_id: str) -> MessageDTO:
        messages = await self._load_messages(session_id)
        for message in messages:
            if message.message_id == message_id:
                return message
        raise ValueError(f"Message {message_id} not found in session {session_id}")

    async def create(self, session_id: str, message_create: MessageCreateRequest, *, thread_id: str | None = None) -> MessageDTO:
        """创建一条用户消息 DTO。

        正文由 accept_user_message 在派发前持久化；此处只生成稳定身份并保存附件。
        """
        if message_create.role != MessageRole.user:
            raise ValueError(
                "创建并执行新一轮消息时 role 必须为 user；"
                "委派、跨会话和团队消息的来源请写入 metadata"
            )
        thread_id = thread_id or self._resolve_main_thread_id(session_id)
        message_id = create_prefixed_id("msg")
        attachments = message_create.attachments
        if self._attachment_store is not None:
            attachments = await self._attachment_store.persist_inline(
                session_id, attachments, thread_id=thread_id, message_id=message_id,
            )
        if attachments:
            content_service = AttachmentContentService(
                workspace_root=self._workspace_root, attachment_store=self._attachment_store,
            )
            attachments = [content_service.resolve_reference(attachment) for attachment in attachments]
        now = datetime.now(UTC)
        return MessageDTO(
            message_id=message_id,
            session_id=session_id,
            thread_id=thread_id,
            role=message_create.role,
            content=message_create.content,
            attachments=attachments,
            metadata=message_create.metadata,
            created_at=now,
            updated_at=now,
        )

    def accept_user_message(
        self, message: MessageDTO, *, execution_identity: UserExecutionIdentity,
        queue_input: ExecutionQueueInput,
    ) -> ExecutionQueueRecord:
        """在 Job 入队前冻结用户输入，并原子建立 acceptance、Turn/root 和 initial execution。"""
        if message.role != MessageRole.user or message.thread_id is None:
            raise ValueError("接受用户输入必须提供真实 user 消息和 thread_id")
        accept = getattr(self._checkpointer, "accept_turn", None)
        if not callable(accept):
            raise TypeError("MessageService 缺少 v2 Turn acceptance owner")
        metadata = dict(message.metadata)
        metadata.pop("display_content", None)
        metadata.pop("boxteam_session_model", None)
        built = UserContentBuilder(attachment_service=AttachmentContentService(
            workspace_root=self._workspace_root, attachment_store=self._attachment_store,
        )).build(
            message.content, list(message.attachments),
        )
        response = build_human_response_metadata(
            message_id=message.message_id,
            display_content=None,
            attachments=built.attachments,
            message_created_at=message.created_at.isoformat(),
            message_metadata=metadata,
            attachment_diagnostics=built.diagnostics,
        )
        payload = user_input_payload(built.content, response.get("attachments"))
        accepted = accept(
            message.session_id, thread_id=message.thread_id,
            accepted_ingress_id=message.message_id,
            acceptance_idempotency_key=f"message:{message.message_id}",
            job_id=execution_identity.job_id,
            admission_idempotency_key=execution_identity.admission_idempotency_key,
            payload=payload,
            payload_kind="text" if isinstance(payload, str) else "structured_content",
            turn_id=execution_identity.turn_id,
            root_item_id=f"item-{message.message_id}",
            extensions=message_annotations(response), queue_input=queue_input,
        )
        if self._attachment_store is not None and accepted["job_id"] == execution_identity.job_id:
            self._attachment_store.complete_message_ingests(
                message.session_id, thread_id=message.thread_id, message_id=message.message_id,
                messages=self._checkpointer,
            )
        return self._checkpointer.execution_queue_record(
            message.session_id, message.thread_id, accepted["job_id"],
        )

    def message_submission(
        self, session_id: str, thread_id: str, operation_id: str, *,
        submission_hash: str | None = None,
    ) -> ExecutionQueueRecord | None:
        """读取原准入与队列输入，不复制业务正文或建立操作账本。"""
        if self._checkpointer is None:
            raise RuntimeError("消息操作查询缺少 canonical input owner")
        record = self._checkpointer.message_submission(session_id, thread_id, operation_id)
        if record is not None and (record.input.submission_hash is None or (
            submission_hash is not None and record.input.submission_hash != submission_hash
        )):
            raise MessageSubmissionConflictError("operation_id 已用于不同消息输入或非用户提交")
        return record

    def accept_job_input(self, job: JobRuntimeStateProtocol, *, queue_input: ExecutionQueueInput) -> ExecutionQueueRecord:
        """真实 Job 入队前完成唯一次准入；已有输入只按完整 identity 核验。"""
        saver = self._checkpointer
        if saver is None:
            raise RuntimeError("MessageService 缺少 Job input owner")
        binding = job.execution_binding
        if isinstance(binding, UserExecutionIdentity):
            # 正文与调度输入共用原快照；不在同次同步准入内重新打开、定位同一个库。
            with saver.open_user_input_snapshot(job.session_id, job.thread_id, job.job_id) as snapshot:
                admission, frozen = saver.accepted_user_message(
                    job.session_id, thread_id=job.thread_id,
                    job_id=job.job_id, message_id=job.message_id, snapshot=snapshot,
                )
                dto = checkpoint_message_to_dto(job.session_id, job.thread_id, 0, frozen)
                if (admission.turn_id != binding.turn_id
                        or admission.admission_idempotency_key != binding.admission_idempotency_key
                        or dto.content != job.message or dto.attachments != job.attachments):
                    raise ValueError("Job 入队请求与已接受用户输入不一致")
                record = saver.execution_queue_record(job.session_id, job.thread_id, job.job_id, snapshot=snapshot)
            if self._attachment_store is not None:
                self._attachment_store.complete_message_ingests(
                    job.session_id, thread_id=job.thread_id, message_id=job.message_id, messages=saver,
                )
            return record
        if not isinstance(binding, InternalExecutionIntent):
            raise TypeError("Job input owner 只接受 typed execution binding")
        if binding.derived_reference is not None:
            raise ValueError("摘要派生 execution 不得作为普通 Agent Job 提交")
        if binding.input_reference_json is not None and job.attachments:
            raise ValueError("seed/reference 准入不得携带未登记的附件")
        if "canonical_item_manifests" in job.message_metadata:
            response = dict(job.message_metadata)
            if attachments_from_message_metadata(response) != job.attachments:
                raise ValueError("内部 Job 附件与已接受 canonical 输入不一致")
            # runtime 已准入的后台优先级归原队列，不被 Job 的默认前台值覆盖。
            frozen = saver.execution_queue_record(job.session_id, job.thread_id, job.job_id)
            queue_input = replace(queue_input, background=frozen.input.background)
        else:
            built = UserContentBuilder(attachment_service=AttachmentContentService(
                workspace_root=self._workspace_root, attachment_store=self._attachment_store,
            )).build("", job.attachments)
            response = build_human_response_metadata(
                message_id=job.message_id, display_content=None,
                attachments=built.attachments, message_created_at=job.message_created_at,
                message_metadata=dict(job.message_metadata), attachment_diagnostics=built.diagnostics,
            )
            if built.attachments:
                response["attachment_blocks"] = built.content
        saver.admit_internal_execution(
            job.session_id, thread_id=job.thread_id, job_id=job.job_id,
            intent=binding, queue_input=queue_input,
            message=(None if binding.input_reference_json is not None else HumanMessage(
                id=job.message_id, content=job.message, response_metadata=response,
            )),
        )
        if self._attachment_store is not None:
            self._attachment_store.complete_message_ingests(
                job.session_id, thread_id=job.thread_id, message_id=job.message_id,
                messages=saver,
            )

        return saver.execution_queue_record(job.session_id, job.thread_id, job.job_id)

    def accepted_job_message(
        self, reference: PendingRequestControl, *, display: bool,
    ) -> MessageDTO:
        """FIFO 恢复和展示共用 canonical 输入投影；队列引用不提供正文。"""
        saver = self._checkpointer
        if saver is None:
            raise RuntimeError("MessageService 缺少 canonical input reader")
        binding = reference.execution_binding
        if isinstance(binding, UserExecutionIdentity):
            admission, human = saver.accepted_user_message(
                reference.session_id, thread_id=reference.thread_id,
                job_id=reference.job_id, message_id=reference.message_id,
            )
        else:
            admission, human = saver.accepted_internal_message(
                reference.session_id, thread_id=reference.thread_id,
                job_id=reference.job_id, message_id=reference.message_id, intent=binding,
            )
        if (admission.admission_idempotency_key != binding.admission_idempotency_key
                or admission.turn_id != reference.turn_id):
            raise ValueError("待处理队列引用与权威 admission 不一致")
        dto = checkpoint_message_to_dto(reference.session_id, reference.thread_id, 0, human)
        if display or isinstance(binding, UserExecutionIdentity):
            return dto
        if not isinstance(human.content, str):
            raise TypeError("冻结内部输入必须投影为文本")
        return dto.model_copy(update={"content": human.content, "metadata": dict(human.response_metadata)})

    async def list_agent_state_records(
        self,
        session_id: str,
        *,
        strict: bool = False,
    ) -> list[dict[str, object]]:
        raw_messages = await self._load_raw_messages(session_id, strict=strict)
        if self._canonical_item_reader is not None:
            canonical_items = await run_sync_shielded(
                self._canonical_item_reader,
                session_id,
                self._resolve_main_thread_id(session_id),
            )
            raw_messages = merge_canonical_reasoning(
                raw_messages,
                canonical_items,
            )
        return await run_sync_shielded(project_agent_state_records, raw_messages, context_name="Agent State")

    async def get_agent_context_state(
        self, session_id: str, thread_id: str
    ) -> AgentContextState:
        """读取 Saver 已提交的唯一消息选择，不再二次解释数组 offset。"""
        if self._checkpointer is None:
            raise RuntimeError("MessageService 未配置 checkpointer，无法读取 Agent Context")

        checkpoint_tuple = await self.load_checkpoint_tuple(session_id, thread_id)
        if checkpoint_tuple is None:
            return {
                "records": [],
                "checkpoint_id": "",
                "raw_message_count": 0,
                "compacted": False,
                "compaction_cutoff": None,
            }

        checkpoint = checkpoint_tuple.checkpoint
        channel_values = checkpoint.get("channel_values", {})
        if not isinstance(channel_values, Mapping):
            raise TypeError(
                "LangGraph checkpoint channel_values 应为 mapping，"
                f"实际类型: {type(channel_values).__name__}"
            )
        raw_messages = channel_values.get("messages", [])
        if not isinstance(raw_messages, list):
            raise TypeError(
                f"Agent Context messages 应为 list，实际类型: {type(raw_messages).__name__}"
            )

        compacted = any(getattr(message, "response_metadata", {}).get("compaction_summary") is True for message in raw_messages)
        raw_count: int | None = len(raw_messages)
        if compacted:
            count_original = getattr(self._checkpointer, "raw_message_count", None)
            if not callable(count_original):
                raise TypeError("MessageService 缺少 canonical 原消息计数 owner")
            anchors = tuple(canonical_message_anchor(message) for message in raw_messages)
            if any(anchor is None for anchor in anchors):
                raise ValueError("compaction-source-unavailable: 消息投影缺少精确 canonical anchor")
            raw_count = await run_sync_shielded(count_original, session_id, thread_id, item_ids=anchors)
        records = await run_sync_shielded(project_agent_state_records, raw_messages, context_name="Agent Context")

        return {
            "records": records,
            "checkpoint_id": str(checkpoint.get("id") or ""),
            "raw_message_count": raw_count,
            "compacted": compacted,
            "compaction_cutoff": None,
        }

    async def get_agent_state_messages(self, session_id: str) -> AgentStateMessagesDTO:
        records = await self.list_agent_state_records(session_id, strict=True)

        def encode_jsonl() -> str:
            return "\n".join(
                json.dumps(public_message_record(record), ensure_ascii=False, separators=(",", ":"))
                for record in records
            )

        return AgentStateMessagesDTO(
            session_id=session_id,
            message_count=len(records),
            jsonl=await run_sync_shielded(encode_jsonl),
        )

    def execution_output_for_job(self, session_id: str, *, thread_id: str, job_id: str) -> tuple[str, InternalDisplayProvenance]:
        if self._checkpointer is None:
            raise RuntimeError("执行结果读取缺少 checkpoint owner")
        admission = self.execution_admission_for_job(session_id, thread_id=thread_id, job_id=job_id)
        if admission is None:
            raise RuntimeError(f"Job 缺少真实 execution admission: {job_id}")
        return self._checkpointer.execution_output(session_id, thread_id, admission.execution_id)

    def execution_admission_for_job(self, session_id: str, *, thread_id: str, job_id: str):
        if self._checkpointer is None:
            raise RuntimeError("MessageService 未配置 execution admission owner")
        return self._checkpointer.execution_admission_for_job(session_id, thread_id=thread_id, job_id=job_id)

    def submit_system_reminder(
        self,
        *,
        session_id: str,
        thread_id: str,
        execution_id: str,
        reminder: str,
        response_metadata: dict[str, object],
        checkpoint_source: str,
        event_identity: str | None = None,
    ) -> bool:
        if self._checkpointer is None:
            raise RuntimeError("MessageService 未配置 checkpointer，无法写入 system_reminder")
        return submit_runtime_reminder(
            checkpointer=self._checkpointer,
            session_id=session_id,
            thread_id=thread_id,
            reminder=reminder,
            response_metadata=response_metadata,
            checkpoint_source=checkpoint_source,
            event_identity=event_identity,
            execution_id=execution_id,
        )

    def request_internal_execution_interrupt(
        self, admission: ExecutionAdmission, *, interrupt_request_id: str,
        reminder: str, response_metadata: dict[str, object],
    ) -> InternalExecutionInterrupt | str:
        if self._checkpointer is None:
            raise RuntimeError("MessageService 缺少 internal interrupt owner")
        decision = build_runtime_reminder_decision(
            session_id=admission.session_id, thread_id=admission.thread_id,
            execution_id=admission.execution_id, reminder=reminder,
            response_metadata=response_metadata, checkpoint_source="user_interrupt",
            event_identity=interrupt_request_id,
        )
        request = InternalExecutionInterrupt(admission, interrupt_request_id, decision.item_id)
        return self._checkpointer.request_internal_execution_interrupt(request, decision)

    def internal_execution_interrupt(self, admission: ExecutionAdmission) -> InternalExecutionInterrupt | None:
        if self._checkpointer is None:
            raise RuntimeError("MessageService 缺少 internal interrupt owner")
        return self._checkpointer.internal_execution_interrupt(admission)

    async def _load_raw_messages(
        self,
        session_id: str,
        *,
        strict: bool = False,
    ) -> list[object]:
        if self._checkpointer is None:
            if strict:
                raise RuntimeError("MessageService 未配置 checkpointer，无法读取 Agent State")
            return []

        tup = await self.load_checkpoint_tuple(
            session_id, self._resolve_main_thread_id(session_id)
        )
        if tup is None:
            return []

        raw_messages = tup.checkpoint.get("channel_values", {}).get("messages", [])
        if not isinstance(raw_messages, list):
            if not strict:
                return []
            raise TypeError(
                f"Agent State messages 应为 list，实际类型: {type(raw_messages).__name__}"
            )

        return raw_messages

    async def _load_messages(self, session_id: str) -> list[MessageDTO]:
        if self._checkpointer is None:
            return []
        thread_id = self._resolve_main_thread_id(session_id)
        checkpoint_tuple = await self.load_checkpoint_tuple(session_id, thread_id)
        if checkpoint_tuple is None:
            return []
        raw_messages = checkpoint_tuple.checkpoint.get("channel_values", {}).get(
            "messages", []
        )
        if not isinstance(raw_messages, list):
            return []
        return self.visible_messages_from_checkpoint(
            session_id,
            thread_id,
            checkpoint_tuple,
        )

    def visible_messages_from_checkpoint(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_tuple: CheckpointTuple,
    ) -> list[MessageDTO]:
        raw_messages = checkpoint_tuple.checkpoint.get("channel_values", {}).get(
            "messages", []
        )
        if not isinstance(raw_messages, list):
            return []

        result: list[MessageDTO] = []
        seen_visible_messages: set[tuple[str, str]] = set()
        for index, message in enumerate(raw_messages):
            if not isinstance(message, BaseMessage):
                continue
            # 普通消息列表只暴露用户可见的输入和最终回复；工具调用、
            # system_reminder 与空 assistant 仍可通过 Agent State 调试视图查看。
            if not is_user_visible_message(message):
                continue
            dto = checkpoint_message_to_dto(session_id, thread_id, index, message)
            visible_key = (dto.role.value, dto.message_id)
            if visible_key in seen_visible_messages:
                continue
            seen_visible_messages.add(visible_key)
            result.append(dto)
        return result
