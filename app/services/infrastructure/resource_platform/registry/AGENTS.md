# 目录用途

`resource_platform/registry/` 存放来源发布与业务 owner 之间的 reaction 接线
(`context_source_reactor.py`)与语义 `ResourceRegistry`(`semantic_registry.py`):
前者把来源 owner 的轻量 change 通知转换成业务 owner 的待观察标记并持有订阅
释放句柄;后者发布并管理语义资源的 immutable snapshot/revision、稳定 identity、
display URI 与 source lineage。二者都不是文件快照 owner,也不解释业务语义。

# 可修改内容

- 可以维护「通知 → 待观察标记 → 权威快照消费」的内存接线实现。
- 可以维护订阅绑定、按来源 identity 去重和释放逻辑。
- 可以维护语义 registry 的 CAS 发布、descriptor 登记与 last-valid 保留语义。
- 可以补充针对本目录接线的单元测试与诊断字段。

# 不可修改内容

- 不得在事件回调里读取文件、访问网络或执行任何 I/O。
- 不得写 ContextStore/checkpoint、构造第二个 ContextStore writer 或第二套 dispose 抽象。
- 不得把正文、宿主机物理路径或 credential 放进通知、快照或诊断输出。
- 不得决定 Skill/AGENTS 的业务注入规则或 wire role;不得持有 locator/handle。

# 规范

- 订阅必须由持有它的 `LifetimeScope` 释放；释放后事件不得再进入业务 owner。
- 消费正文一律回到来源 owner 发布的权威内存快照，不重新读盘。
- 失败必须显式抛出并带来源 identity；不得静默跳过不可用来源。
- 语义 revision 由 facet payload 的 JCS hash 决定;发布必须 CAS 幂等,unavailable 保留 last-valid 可审计。
