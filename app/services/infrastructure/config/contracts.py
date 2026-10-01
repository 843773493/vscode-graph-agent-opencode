"""`config` 与 `config_service` 共同依赖的配置契约类型（中性层，不成环）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.services.infrastructure.config.snapshot import ConfigSnapshot

# 候选快照应用器：提交前把 `previous` → `candidate` 应用到运行态。`config` 包的
# `ConfigSnapshotStore.reload` 与 `config_service` 的 `ConfigReloadMixin` 共用此别名。
# 落在此中性层（只依赖 `config.snapshot`）是因为 `config` 不得反向依赖 `config_service`：
# 若 `config/store.py` 反向 import `config_service_common`，则 `config.__init__` → `store`
# → `config_service.__init__` → `config` 形成导入环。
ConfigCandidateApplier = Callable[[ConfigSnapshot, ConfigSnapshot], Awaitable[None]]
