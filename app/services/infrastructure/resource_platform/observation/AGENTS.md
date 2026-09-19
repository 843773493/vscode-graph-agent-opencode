# 目录用途

`resource_platform/observation/` 承载资源观察侧的轻量通知边界：只传递来源 identity、revision 与可用性等内存事实，供业务 owner 在事件到达时排队 reaction。它不读取来源正文，也不决定业务上下文。

# 可修改内容

- 可以维护观察通知值对象、订阅 handle 与按订阅者隔离的轻量通知通道。
- 可以扩展不携带正文/credential 的通道合同（队列上限、gap、订阅生命周期）。
- 可以补充针对本目录通道的单元测试与诊断字段。

# 不可修改内容

- 不得在通知事件中携带来源正文、宿主机物理路径或 credential。
- 不得建立第二个通用 EventBus、durable 事件账本或与 Job 队列混用的通道。
- 不得在此目录读取文件、写 ContextStore/checkpoint 或决定 Skill/AGENTS 的业务注入规则。
- 不得把通知当作 durable 事实；consumer 必须回到来源 owner 发布的权威内存快照。

# 规范

- 通知消费者回调必须保持零 I/O：只做内存标记与排队。
- 队列溢出必须显式暴露 gap，不得静默丢弃变化。
- 订阅由持有它的 owner 通过 `LifetimeScope` 释放，不允许自行创建第二套 dispose 抽象。