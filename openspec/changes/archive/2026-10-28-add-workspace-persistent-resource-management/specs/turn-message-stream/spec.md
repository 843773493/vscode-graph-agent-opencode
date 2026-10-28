## MODIFIED Requirements

### Requirement: [Runtime] Persistent resources have independent lifecycle and operation leases

每类跨 Turn 的 terminal、browser context、MCP connection、development server 等外部资源 MUST 由其资源领域 owner 管理唯一权威状态和实际生命周期。通用资源服务/Registry 只能路由列表、详情和控制请求，不得保存第二份资源事实、推断工具参数、持有 `cleanup_policy` 或凭进程内 stopper 宣称外部资源已停止。`TurnExecutionScope` 只能持有当前操作 scope 和由领域 owner 签发的 operation lease；取消、超时、前端断线和 Thread runtime unload MUST NOT 默认销毁工作区持久资源。Thread 范围资源由领域 owner 根据保留范围、其它关联和有效 lease 执行清理；持久资源的停止或删除 MUST 经领域 owner 核验后完成。

#### Scenario: Turn 中断只终止当前操作，不销毁持久资源

- **WHEN** Turn 正在持有一个 terminal server 或 browser context 的 operation lease，并在其中执行命令或导航时收到用户中断
- **THEN** AgentLoop 中止当前操作并由 owner 释放该 operation lease；资源状态由其领域 owner 持有，`stream.interrupted` 不表示资源已销毁或操作副作用已回滚

#### Scenario: 资源显式停止独立于 Turn 取消

- **WHEN** 用户或工作区控制面请求停止一个持久资源
- **THEN** 对应领域 owner 独立校验 `resource_id`、有效 lease/关联和资源状态后执行并核实 stop；不能通过调用 Turn 的 `CancellationSignal` 代替资源 stop，也不能因为 SSE 断开隐式 stop

#### Scenario: 资源操作结果按 lease 对账

- **WHEN** 一个资源操作的异步结果到达，且对应 Turn 可能已经取消、崩溃或释放 lease
- **THEN** owner 按 `resource_id`、`lease_id` 和 `operation_id` 校验结果；过期结果不能重新启动工具或改变已终态 Turn，未知结果保留为资源操作未知事实

#### Scenario: 崩溃恢复不盲目终止持久资源

- **WHEN** 后端在持久资源操作或 Turn scope 存活期间崩溃
- **THEN** 领域 owner 恢复并核实资源和 lease，区分已恢复、孤立与结果未知状态；不得因为 Turn 崩溃自动杀掉所有持久资源或自动重放未知操作
