"""`config` 与 `config_service` 共同依赖的配置契约类型（中性层，不成环）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.services.infrastructure.config.snapshot import ConfigSnapshot

# 候选运行时预检／应用器；权威发布由下面的快照发布器协调。
# 落在此中性层（只依赖 `config.snapshot`）是因为 `config` 不得反向依赖 `config_service`：
# 若 `config/store.py` 反向 import `config_service_common`，则 `config.__init__` → `store`
# → `config_service.__init__` → `config` 形成导入环。
ConfigCandidateApplier = Callable[[ConfigSnapshot, ConfigSnapshot], Awaitable[None]]

# 发布回调只执行同步的内存切换，不能在 SQLite 成功与切换之间再等待 I/O。
ConfigPublication = Callable[[], None]
ConfigSnapshotPublisher = Callable[
    [ConfigSnapshot, ConfigSnapshot, ConfigPublication], Awaitable[None]
]
