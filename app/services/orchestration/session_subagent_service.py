"""SessionSubagentService —— Agent 委派 → owner Session 内 durable child thread
（OpenSpec 8.5-B，R25 单链路）。

R25 起委派不再创建 delegated child Session：``delegate``` 经装配好的
``OwnerThreadCreationFactory``` 取得绑定 parent Session 的
``ThreadCreationService```，创建 ``kind=child``` durable child thread
并提交 pending admission intent（R23 状态机）。红线：

- R25 **不启动真实 child Job**：intent 保持 ``pending```，由 R26
  thread-qualified binder 消费；本服务不触碰 JobService/orchestrator。
- 同一委派的 identity 由 (parent session, parent job, parent tool call,
  subagent type) 确定性派生：同 tool call 崩溃重试收敛同一 child thread
  与 intent；不同 preimage / aborted 后重登记由 store/ledger fail closed
  （同 delegation 不换绑）。
- delegation/member/task/role/coordinator 冻结进 owner session-control 的
  collaboration ledger（revision CAS），parent 线程/Job/tool call/subagent
  type/title/task seed/可信上下文冻结进 creation preimage。
- 失败直接抛错，不做 adapter/fallback/双写。
"""

from __future__ import annotations

import hashlib

from app.abstractions.session_subagent import (
    GENERAL_PURPOSE_SUBAGENT,
    BeforeSubagentStart,
    OwnerThreadCreationFactoryProtocol,
    SessionReaderProtocol,
    SessionSubagentAccepted,
)
from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)

__all__ = ["SessionSubagentService"]

# child thread 能力 profile：R25 冻结产品 canonical profile（与 main thread
# 同一 GraphBinding 四元组，保证 R26/R28 restore 可解析）；child 专属
# runtime policy（如禁用 Goal）归 R26 ThreadRuntimePolicy 轮决策。
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
    ) -> None:
        self._parent_session_reader = parent_session_reader
        self._thread_creation_factory = thread_creation_factory

    async def delegate(
        self,
        *,
        parent_session_id: str,
        parent_agent_id: str,
        parent_job_id: str,
        parent_tool_call_id: str,
        description: str,
        subagent_type: str,
        title: str | None = None,
        trusted_context: dict[str, object] | None = None,
        untrusted_instructions: str | None = None,
        before_start: BeforeSubagentStart | None = None,
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
        parent_thread_id = self._thread_creation_factory.owner_main_thread_id(
            parent_session_id
        )
        normalized_title = self._build_title(title or normalized_description)
        task_seed: dict[str, object] = {
            "description": normalized_description,
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
            },
        )
        if result.admission_state != "pending":
            # R25 契约：创建流只提交 pending intent；bound 属 R26 binder。
            raise RuntimeError(
                "委派 child thread 的 admission intent 非 pending（状态机被"
                f"绕过，fail closed）: delegation_id={delegation_id!r}, "
                f"state={result.admission_state!r}"
            )
        accepted = SessionSubagentAccepted(
            owner_session_id=parent_session_id,
            child_thread_id=result.child_thread_id,
            delegation_id=delegation_id,
            admission_idempotency_key=delegation_id,
            admission_state=result.admission_state,
            execution_binding_id=result.execution_binding_id,
            frozen_job_id=result.frozen_job_id,
        )
        if before_start is not None:
            try:
                await before_start(accepted)
            except Exception as error:
                raise RuntimeError(
                    "委派 child thread 已创建，但启动前准备失败: "
                    f"child_thread_id={result.child_thread_id} "
                    f"delegation_id={delegation_id} error={error}"
                ) from error
        return accepted

    @staticmethod
    def _build_title(description: str) -> str:
        single_line = " ".join(description.split())
        return f"委派：{single_line[:48]}"
