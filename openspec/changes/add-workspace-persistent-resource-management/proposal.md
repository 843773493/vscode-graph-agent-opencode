## Why

当前 Browser、Terminal 等资源按 Session/Thread 展示，但它们的外部状态由各自领域 owner 持有。Thread runtime idle unload 不等于只卸载 Agent graph、让 Browser/Terminal 进程继续存活：临时资源也必须随 Thread 释放而回收。为防止 Agent 忘记清理，资源创建工具默认创建非持久资源；用户或明确的工具参数可以选择持久化，或将已有临时资源提升为工作区资源。现有 `turn-message-stream` 仍把停止策略交给通用 `ResourceManager`/`cleanup_policy`，无法表达资源 owner、Thread 关联与用户持久化之间的边界。

## What Changes

- 定义 Browser、Terminal 等领域 owner 为资源状态、外部副作用、工作区持久记录和 Thread 关联的唯一权威；通用 Registry 只聚合、路由和投影，不另存生命周期事实。
- 将资源保留范围明确为 Thread 或工作区。Browser、Terminal 等 Agent 资源创建工具的 `retention_scope` 默认是 `thread`；只有用户选择或工具参数明确指定 `workspace` 时才持久化。Thread 范围资源可由用户或显式工具操作提升为工作区资源；Thread unload、Thread 删除或来源 Session 删除都不能销毁工作区资源。
- 定义 Thread unload、Thread 删除和 Session 删除向领域 owner 发出的幂等释放/清理通知。Thread unload 或删除时，owner 先释放句柄和已完成的 operation lease，再解除临时资源与 Thread 的关联，并立即停止、核实和删除该资源；工作区资源只解除该 Thread 的关联并继续保留。显式 detach 后遗留的 Thread 范围资源若连续 30 分钟没有任何 Thread 关联且没有有效 operation lease，owner 自动停止、核实并删除，作为遗漏 delete 的回收兜底。
- 为用户提供工作区级资源查询、详情、持久化、Thread attach/detach、显式 stop 和 delete 操作；attach/detach 表示资源与 Thread 的业务关联，不表示浏览器客户端或 WebSocket 的连接状态。Thread 关联归零后的 30 分钟回收只适用于 Thread 范围资源，不是 Thread runtime 的 idle unload 阈值。
- 提供可从原 Session 独立访问的资源管理入口，既能管理工作区持久资源，也能在 30 分钟宽限期内发现并重新关联或持久化临时孤儿。Session 资源视图展示其关联；资源按类型归属的 UI 区域展示。前端成功时采用 API 的完整结果，失败时重新读取权威状态并展示错误。
- **BREAKING** 修改 `turn-message-stream` 的 `[Runtime] Persistent resources have independent lifecycle and operation leases` 合同：移除通用 `ResourceManager`、`cleanup_policy` 和进程内 stopper 对外部资源停止状态的决定权，统一由领域 owner 按保留范围、关联与有效 lease 执行并核实。
- 复用 `add-context-injection-lifecycle` 定义的 `LifetimeScope` 和持久 operation lease；Thread residency、idle unload 资格与阈值由 `add-itemized-rollout-context` 唯一规定。本 change 不另造 Thread residency 或 dispose 机制。Thread graph 的懒加载和驻留复用也由该 change 维护。

## Capabilities

### New Capabilities
- `workspace-persistent-resources`: 定义工作区级资源权威、Thread 关联与持久化、owner 清理/恢复及用户管理 API。

### Modified Capabilities
- `turn-message-stream`: 将跨 Turn 外部资源的生命周期与停止权归还领域 owner，移除通用 `cleanup_policy` 停止语义。

## Impact

- 影响 Browser、Terminal 及其它跨 Turn 资源领域 owner、`SessionResourceProviderRegistry`、工作区 API 与各资源所属的前端展示区域。
- 持久资源记录必须位于工作区 `${workspace_abs_path}/.boxteam/` 下的 owner 存储，不能依赖来源 Session 目录；Session/Thread 只保留可清理的关联。
- `add-itemized-rollout-context` 唯一拥有 Thread 身份、GraphBinding、runtime generation、准入和 Thread runtime 的 30 分钟 residency policy；本 change 的另一个 30 分钟计时只回收零 Thread 关联的临时资源。`add-context-injection-lifecycle` 定义 `LifetimeScope` 的进程内释放和 context state 保留，资源 change 只消费两者合同。
- 本 change 不实现后台任务/mailbox 的 checkpoint 恢复；该合同由 `add-itemized-rollout-context` 中的 Thread durable task/mailbox requirement 定义。资源 change 只消费该 Thread identity、lease 和恢复边界，不自行重放外部副作用。
