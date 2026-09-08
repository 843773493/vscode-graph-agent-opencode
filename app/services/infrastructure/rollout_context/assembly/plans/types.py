"""注册生命周期的只读结果；历史导入不伪造运行时 draft。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.itemized.request_plan import ContextRequestPlan


@dataclass(frozen=True, slots=True)
class ContextPlanRegistration:
    """draft 仅属于真实创建记录，导入来源只允许读取已封存的 snapshot。"""

    draft: ContextRequestPlan | None
    creation_hash: str | None
    revision: int
    plan_state: str
    assembly_id: str | None
    seal_idempotency_key: str | None
    seal_hash: str | None
    registration_origin: str = "runtime"
    source_provenance: Mapping[str, object] | None = None
    seal_input_hash: str | None = None
    source_manifest: Mapping[str, object] | None = None
