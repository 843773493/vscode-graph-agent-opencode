"""Agent 委派到 owner Session 内的 durable child thread。

creation owner 冻结 parent/Job/tool call/seed/reference 与 collaboration
身份，生产 binder 准入真实 child Job。同一委派重试收敛同一 thread、intent
和 execution；缺 binder 时在任何持久化副作用前明确报告 unavailable。
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

from app.abstractions.session_subagent import (
    GENERAL_PURPOSE_SUBAGENT,
    OwnerThreadCreationFactoryProtocol,
    SessionReaderProtocol,
    SessionSubagentAccepted,
)
from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBinder,
)

__all__ = [
    "SessionSubagentService",
    "SessionSubagentUnavailableError",
]


class SessionSubagentUnavailableError(RuntimeError):
    """缺少生产 binder 时，携带冻结 delegation identity 明确报告失败。"""

    def __init__(
        self,
        *,
        delegation_id: str,
        parent_session_id: str,
        subagent_type: str,
    ) -> None:
        super().__init__(
            "委派 child thread unavailable：生产未装配真实 thread binder，"
            "初始 execution admission intent 无法绑定（fail closed，"
            "绝不静默停留 pending、绝不伪造 execution）: "
            f"admission_idempotency_key={delegation_id} "
            f"owner_session_id={parent_session_id} subagent_type={subagent_type}"
        )
        self.delegation_id = delegation_id
        self.parent_session_id = parent_session_id
        self.subagent_type = subagent_type

# child 与 main thread 共用产品 canonical profile；child 专属运行策略
# 由 ThreadRuntimePolicy 决定，恢复时仍使用同一 GraphBinding 四元组。
_CHILD_GRAPH_BINDING = DEEP_AGENT_GRAPH_BINDING
_CHILD_CAPABILITY_PROFILE = dict(DEEP_AGENT_CAPABILITY_PROFILE)


def _derive_delegation_id(
    *,
    parent_session_id: str,
    parent_job_id: str,
    parent_tool_call_id: str,
    subagent_type: str,
) -> str:
    """确定性派生稳定 delegation_id（同 tool call 重试收敛同一委派）。

    以委派的逻辑身份四元组为唯一熵源做 sha256 截断——不依赖当前时间或
    随机数；idempotency key（creation/admission）直接取该 delegation_id
    （hex 安全单段路径名）。
    """
    material = (
        f"session-subagent-delegation\x00{parent_session_id}"
        f"\x00{parent_job_id}\x00{parent_tool_call_id}"
        f"\x00{subagent_type}"
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:32]
    return f"del_{digest}"


class SessionSubagentService:
    """把 Agent 委派转换为 owner Session 内的 durable child thread。"""

    def __init__(
        self,
        *,
        parent_session_reader: SessionReaderProtocol,
        thread_creation_factory: OwnerThreadCreationFactoryProtocol,
        initial_execution_binder: InitialExecutionBinder | None,
        bind_owner: Callable[[str], Awaitable[None]],
    ) -> None:
        self._parent_session_reader = parent_session_reader
        self._thread_creation_factory = thread_creation_factory
        # 真实 thread binder 是生产必须显式声明的依赖（OpenSpec 主 spec
        # 第 244 行义务）。binder 缺省为 None 时本服务 fail closed，绝不向
        # 模型返回虚假 accepted+绑定承诺；intent 不会永久滞留。
        self._initial_execution_binder = initial_execution_binder
        self._bind_owner = bind_owner

    async def delegate(
        self,
        *,
        parent_session_id: str,
        parent_thread_id: str,
        parent_agent_id: str,
        parent_job_id: str,
        parent_tool_call_id: str,
        description: str,
        subagent_type: str,
        title: str | None = None,
        trusted_context: dict[str, object] | None = None,
        untrusted_instructions: str | None = None,
        collaboration_member: dict[str, object] | None = None,
    ) -> SessionSubagentAccepted:
        normalized_description = description.strip()
        if not normalized_description:
            raise ValueError("description 不能为空")
        if subagent_type != GENERAL_PURPOSE_SUBAGENT:
            raise ValueError(
                "不支持的 subagent_type: "
                f"{subagent_type}；当前仅支持 {GENERAL_PURPOSE_SUBAGENT}"
            )
        if not parent_job_id:
            raise RuntimeError("创建委派 child thread 时缺少 parent_job_id")
        if not parent_tool_call_id:
            raise RuntimeError("创建委派 child thread 时缺少 parent_tool_call_id")

        parent_session = await self._parent_session_reader.get(parent_session_id)
        if parent_session.current_agent_id != parent_agent_id:
            raise RuntimeError(
                "委派调用的 Agent 与父会话当前 Agent 不一致: "
                f"expected={parent_session.current_agent_id} actual={parent_agent_id}"
            )

        delegation_id = _derive_delegation_id(
            parent_session_id=parent_session_id,
            parent_job_id=parent_job_id,
            parent_tool_call_id=parent_tool_call_id,
            subagent_type=subagent_type,
        )
        if self._initial_execution_binder is None:
            # 没有真实 binder，delegate MUST NOT 创建必然永久 pending 的
            # 初始 execution intent、MUST NOT 向调用方返回 accepted；在
            # 任何持久化副作用前 fail closed（OpenSpec 主 spec 第 244 行）。
            raise SessionSubagentUnavailableError(
                delegation_id=delegation_id,
                parent_session_id=parent_session_id,
                subagent_type=subagent_type,
            )
        self._thread_creation_factory.validate_owner_thread(parent_session_id, parent_thread_id)
        normalized_title = self._build_title(title or normalized_description)
        task_seed: dict[str, object] = {
            "description": normalized_description,
            "agent_id": parent_agent_id,
            "subagent_type": subagent_type,
            "title": normalized_title,
            "parent_job_id": parent_job_id,
            "parent_tool_call_id": parent_tool_call_id,
            "trusted_context": dict(trusted_context or {}),
        }
        if untrusted_instructions is not None:
            task_seed["untrusted_instructions"] = untrusted_instructions
        # preimage 闭集（session_metadata 四字段）：graph_binding /
        # capability_profile / task_seed（委派任务与来源）/ task_reference
        # （parent thread/parent session/delegation）。
        session_metadata: dict[str, object] = {
            "graph_binding": {
                "graph_id": _CHILD_GRAPH_BINDING.graph_id,
                "graph_revision": _CHILD_GRAPH_BINDING.graph_revision,
                "graph_schema_hash": _CHILD_GRAPH_BINDING.graph_schema_hash,
                "capability_profile_hash": compute_capability_profile_hash(
                    capability_profile=_CHILD_CAPABILITY_PROFILE
                ),
            },
            "capability_profile": dict(_CHILD_CAPABILITY_PROFILE),
            "task_seed": task_seed,
            "task_reference": {
                "parent_session_id": parent_session_id,
                "parent_thread_id": parent_thread_id,
                "delegation_id": delegation_id,
            },
        }
        result = await self._thread_creation_factory.for_owner_session(
            parent_session_id
        ).create(
            idempotency_key=delegation_id,
            session_id=parent_session_id,
            thread_id=None,
            delegation_id=delegation_id,
            initial_state="running",
            session_metadata=session_metadata,
            artifact_manifest={},
            collaboration_member={
                "role": "delegated_subagent",
                "subagent_type": subagent_type,
                "title": normalized_title,
                **dict(collaboration_member or {}),
            },
        )
        await self._bind_owner(parent_session_id)
        bound_intent = self._thread_creation_factory.for_owner_session(parent_session_id).control_store.get_initial_execution_intent(delegation_id)
        accepted = SessionSubagentAccepted(
            owner_session_id=parent_session_id,
            child_thread_id=result.child_thread_id,
            delegation_id=delegation_id,
            admission_idempotency_key=delegation_id,
            admission_state=bound_intent.state,
            execution_binding_id=result.execution_binding_id,
            frozen_job_id=result.frozen_job_id,
        )
        return accepted

    @staticmethod
    def _build_title(description: str) -> str:
        single_line = " ".join(description.split())
        return f"委派：{single_line[:48]}"
