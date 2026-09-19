"""受信 Session Skill untrack 服务。

OpenSpec 3.5：除模型 skill_load 工具外，受信 Session 控制 API 必须以精确
session/thread/name 复用生产 CSM 的同一 load_skill(mode="untrack") mutation
和结果合同，作为 tracked-source-unavailable 阻止 dispatch 后的人工恢复入口。
本服务不建立第二状态机、不接受路径、不读取 source。
"""

from __future__ import annotations

from app.core.exceptions import NotFoundError
from app.schemas.internal_v2.session import (
    SessionSkillUntrackErrorDTO,
    SessionSkillUntrackResultDTO,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    MAIN_THREAD_ID,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
    ContextSourceTrackingStateConflict,
)


class SessionSkillTrackingService:
    """以精确 SessionThread 为 owner 复用唯一 CSM untrack mutation。"""

    def __init__(
        self,
        *,
        checkpointer: RolloutCheckpointSaver,
        session_service: SessionService,
    ) -> None:
        self._checkpointer = checkpointer
        self._session_service = session_service

    async def untrack(
        self,
        *,
        session_id: str,
        thread_id: str,
        name: str,
    ) -> SessionSkillUntrackResultDTO:
        owner = ContextSourceOwnerKey(session_id=session_id, thread_id=thread_id)
        if thread_id == session_id:
            # 与生产 owner 解析同一条规则：main thread 必须用 MAIN_THREAD_ID，
            # 裸 session_id 不是合法 thread 身份，提前拒绝保证零状态变化。
            raise ValueError(
                "main thread 必须使用 thread_id='main'，"
                f"不能把裸 session_id 当作 thread_id: {session_id}"
            )
        # 错误 thread 零状态变化：会话不存在（404）或 thread 未在权威
        # thread_catalog 登记（404）都在触碰 rollout owner 之前失败。
        child_threads = await self._session_service.list_child_threads(session_id)
        if thread_id != MAIN_THREAD_ID and thread_id not in {
            item.thread_id for item in child_threads.items
        }:
            raise NotFoundError(
                f"SessionThread 不存在: session_id={session_id}, "
                f"thread_id={thread_id}"
            )
        if not self._checkpointer.context_source_control_owner_available(owner):
            # 合成 SessionThread 没有 durable ContextStore owner，不允许为它
            # 伪造持久化 untrack。
            raise NotFoundError(
                "SessionThread 没有可用的 ContextStore owner: "
                f"session_id={session_id}, thread_id={thread_id}"
            )
        try:
            # 与生产 agent 装配同一条 rehydration 链：owner + 唯一
            # ContextSourceControlStatePort 恢复控制状态，再调用同一 mutation。
            manager = ContextSourceManager(
                owner=owner,
                control_state_port=self._checkpointer,
            )
            receipt = manager.load_skill(name, mode="untrack")
        except ContextSourceTrackingStateConflict as error:
            return self._error_result(
                session_id,
                thread_id,
                name,
                error.code,
                str(error),
            )
        except KeyError as error:
            return self._error_result(
                session_id,
                thread_id,
                name,
                "skill-not-found",
                str(error),
            )
        return SessionSkillUntrackResultDTO(
            session_id=session_id,
            thread_id=thread_id,
            name=receipt.name,
            mode="untrack",
            status=receipt.status,
            display_uri=receipt.display_uri,
            revision=receipt.revision,
            content_hash=receipt.content_hash,
            append_status=receipt.append_status,
            tracked=receipt.tracked,
            queued=receipt.queued,
            error=None,
        )

    @staticmethod
    def _error_result(
        session_id: str,
        thread_id: str,
        name: str,
        code: str,
        message: str,
    ) -> SessionSkillUntrackResultDTO:
        """确定性失败的安全 DTO：零状态变化，不泄露 locator/正文。"""
        return SessionSkillUntrackResultDTO(
            session_id=session_id,
            thread_id=thread_id,
            name=name.strip(),
            mode="untrack",
            status="error",
            append_status="none",
            tracked=False,
            queued=False,
            error=SessionSkillUntrackErrorDTO(code=code, message=message),
        )
