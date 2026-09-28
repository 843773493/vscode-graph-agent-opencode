"""跨层共享的「客户端可触发的状态冲突」领域异常。

本模块只承载一个语义标记类型，不含任何业务逻辑：

- 服务层用它表达「当前资源状态不允许该动作 / 与在途操作冲突」，这类失败**客户端
  可通过重试、修正时序或先查询终态来消除**；
- API 适配层据此按**类型**落 409，绝不按消息字符串匹配（字符串匹配脆弱且禁止）。

为什么继承 ``RuntimeError``：本仓既有服务层统一用 ``RuntimeError`` 表达这类可恢复
冲突（``RuntimeService`` 的生命周期守卫、``ToolTestService`` 的重复启动、
``SessionCatalogService`` 的 cursor/索引冲突等），而同一批服务也可能抛出**真正的**
服务端完整性故障 ``RuntimeError``（账本损坏、索引分叉、父节点缺失）。让本类型继承
``RuntimeError`` 是**类型细化后的向后兼容基类**，不是双轨：

- 仍未类型化的历史调用方继续按 ``RuntimeError`` 捕获，行为不变；
- 已类型化的调用方（API 层）可在 ``except RuntimeError`` 之前先按本类型分流，把完整性
  故障放行成 5xx，从而让「客户端状态冲突」与「服务端完整性故障」在类型上可区分。

严禁在本模块堆积其它无关异常；领域专属冲突类型留在其 owner 模块内（例如
``NavigationMutationConflictError`` 留在 ``session_navigation.queue_store``），本模块
只定义这一个跨层语义基类。
"""

from __future__ import annotations


class ClientStateConflictError(RuntimeError):
    """客户端可消除的状态冲突：API 层按类型落 409。

    典型场景：幂等键撞上既有未完成运行、生命周期状态不允许该动作、乐观并发 revision
    已变化、同 key 异 preimage 冲突、cursor 所绑 revision 已过期。

    不属于本类型语义的失败（内部不变量被破坏、持久化记录损坏、索引分叉等）应继续抛
    裸 ``RuntimeError``，由 API 层放行成 5xx 并保留完整服务端日志。
    """


__all__ = ["ClientStateConflictError"]

