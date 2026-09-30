"""Workspace 状态库 config source layer、source journal 与 fan-out 账本的唯一实现。

本模块承载单一垂直链路的唯一实现：

- ``config_source_layers`` 权威 layer 行（去重同步、CAS、上一版快照与备份路径）；
- ``config_source_journal`` 事件账本与 ``config_source_owner`` generation 水位；
- ``config_source_fanout`` 逐 generation/逐 workspace 的导入结果账本；
- ``source_generation_high_water_mark`` 只读水位查询。

``WorkspaceConfigSourceMixin`` 由
``app.services.infrastructure.workspace_state_store.WorkspaceStateStore`` 继承
装配；宿主负责 ``_WORKSPACE_MIGRATIONS`` 中本族三张表的 DDL 与迁移序号，本模块
只承载读写方法族，宿主提供 ``_database``。宿主 ``__init__`` 之外的其它方法族不调用
本模块私有辅助。

错误分类沿用 workspace_state_store 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` revision/digest/generation CAS 冲突、``RuntimeError``
事务后读取失败。
"""

from app.services.infrastructure.workspace_config_source.workspace_config_source_fanout import (
    WorkspaceConfigSourceFanoutMixin,
)
from app.services.infrastructure.workspace_config_source.workspace_config_source_journal import (
    WorkspaceConfigSourceJournalMixin,
)
from app.services.infrastructure.workspace_config_source.workspace_config_source_layer import (
    WorkspaceConfigSourceLayerMixin,
)

__all__ = ["WorkspaceConfigSourceMixin"]


class WorkspaceConfigSourceMixin(
    WorkspaceConfigSourceLayerMixin,
    WorkspaceConfigSourceJournalMixin,
    WorkspaceConfigSourceFanoutMixin,
):
    """config source layer / journal / fan-out 三族方法族的组合装配点。"""
