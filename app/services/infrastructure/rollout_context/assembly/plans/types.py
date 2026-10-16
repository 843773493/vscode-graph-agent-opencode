"""真实 plan 创建、修订和封存事实的只读结果。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.request_plan import ContextRequestPlan


@dataclass(frozen=True, slots=True)
class ContextPlanRegistration:
    """fork 保留原创建草稿、revision、hash 与 sealed snapshot，不重造 plan。

    revision 从 0 开始，只在 draft 实际变化且修订 CAS 成功后递增；seal 不递增。
    creation_hash 排除顶层 plan_id/plan_hash 及 refs/tool_set_refs 中的 plan_id，
    仍绑定 session/thread owner、source/ref identity 与顺序。
    seal_input_hash 记录封存输入摘要；seal_hash 绑定 sealed snapshot、detail key 与
    seal_input_hash，不能替代 plan_hash 或 request_hash。历史 scope 的只读授权由目标本地 binding 决定。
    """

    session_id: str
    thread_id: str
    draft: ContextRequestPlan
    creation_hash: str
    draft_hash: str
    draft_json: str = field(repr=False)
    revision: int
    plan_state: str
    assembly_id: str | None
    seal_idempotency_key: str | None
    seal_hash: str | None
    seal_input_hash: str | None = None
    sealed_snapshot: ContextAssemblySnapshot | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ContextPlanSeal:
    """同一封存事务消费一次核验结果；规范文本用于核对本次实际写入。"""

    registered: ContextPlanRegistration
    snapshot: ContextAssemblySnapshot
    snapshot_json: str
    seal_idempotency_key: str
    seal_input_hash: str
    detail_key: str | None
    seal_hash: str
