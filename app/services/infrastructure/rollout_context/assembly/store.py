"""Context assembly/detail/overlay persistence owner。

该 mixin 只封装 v2 ContextRequestPlan、sealed assembly、detail manifest、
source overlay 与 selection manifest 的 SQLite 边界；RolloutStorage 提供
连接、锁、路径和提交 port。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import (
    canonical_json_bytes,
)
from app.services.infrastructure.rollout_context.assembly.overlays import (
    ContextOverlayStorageMixin,
)
from app.services.infrastructure.rollout_context.assembly.plans.store import (
    ContextPlanRegistryStorageMixin,
)
from app.services.infrastructure.rollout_context.assembly.reader import (
    ContextAssemblyReaderMixin,
)
from app.services.infrastructure.rollout_context.assembly.registry import (
    AssemblyRegistryMixin,
)
from app.services.infrastructure.rollout_context.assembly.sealing import (
    ContextAssemblySealMixin,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _v2_json(value: object) -> str:
    return _json(value)


class ContextAssemblyStorageMixin(
    ContextPlanRegistryStorageMixin,
    ContextAssemblyReaderMixin,
    ContextAssemblySealMixin,
    ContextOverlayStorageMixin,
    AssemblyRegistryMixin,
):
    @staticmethod
    def _assembly_json(snapshot: ContextAssemblySnapshot) -> str:
        # v2 sealed assembly 是不可变便携副本，与 JSONL envelope 共用
        # RFC 8785/JCS，避免 Python sort_keys 的跨语言浮点/Unicode 差异。
        return _v2_json(snapshot.to_dict())
