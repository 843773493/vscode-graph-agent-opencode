"""`config` 与 `config_service` 共同依赖的配置契约类型（中性层，不成环）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.config.snapshot import ConfigSnapshot

# 发布回调只执行同步的内存切换，不能在 SQLite 成功与切换之间再等待 I/O。
ConfigPublication = Callable[[], None]
ConfigApplyRecorder = Callable[[dict[str, object]], None]

# 候选在原 shadow scope 内准备资源，提交前不可公开；返回同步激活回调。
# 破坏性动作前必须经原 journal recorder 确认 claim 并记录意图。
# 落在此中性层是因为 `config` 不得反向依赖 `config_service`：
# 若 `config/store.py` 反向 import `config_service_common`，则 `config.__init__` → `store`
# → `config_service.__init__` → `config` 形成导入环。
ConfigCandidateApplier = Callable[
    [ConfigSnapshot, ConfigSnapshot, LifetimeScope, ConfigApplyRecorder], Awaitable[ConfigPublication]
]

ConfigSnapshotPublisher = Callable[
    [ConfigSnapshot, ConfigSnapshot, ConfigPublication], Awaitable[None]
]
