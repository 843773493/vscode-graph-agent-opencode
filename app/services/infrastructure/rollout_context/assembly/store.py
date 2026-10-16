"""Context assembly/detail/overlay persistence owner。

该 mixin 只封装 v2 ContextRequestPlan、sealed assembly、detail manifest、
source overlay 与 selection manifest 的 SQLite 边界；RolloutStorage 提供
连接、锁、路径和提交 port。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.store import RolloutContextStore


class ContextAssemblyStore(
    ContextPlanRegistryStorageMixin,
    ContextAssemblyReaderMixin,
    ContextAssemblySealMixin,
    ContextOverlayStorageMixin,
    AssemblyRegistryMixin,
):
    def __init__(self, context: RolloutContextStore) -> None:
        self._context = context
        self._storage = context.storage
