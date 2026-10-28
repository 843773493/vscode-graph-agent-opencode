## Why

当前 Goal 续跑与定时会话生成分别维护生命周期和调度，用户无法复用这些能力组合“脚本等待服务上线后继续”或“由判断 thread 分析后决定是否继续”等持久任务。需要建立工作区级持久任务运行时，并提供可观察、可管理的前端入口，使 Goal 和现有定时任务都成为通用组件构建的具体应用。

## What Changes

- 引入 `PersistentTask` 与 `PersistentTaskRuntime`：解耦触发器、判断器、上下文构造器和执行动作，统一持久化、调度、去重、重启恢复和运行记录。
- 所有任务统一为“触发 → 判断 → 动作”：触发器决定何时判断，判断器决定本次是否执行。普通定时任务是“定时触发＋无附加条件（始终通过）＋动作”的快捷配置，开启脚本或模型条件后仍使用同一任务模型与执行链路。
- 支持定时、执行结束、外部脚本通知和手动触发；支持规则、脚本与独立判断 thread；时间到期由调度器判断，不为检查时间反复调用脚本或模型；区分仅更新上下文和更新上下文后请求执行。
- 所有任务必须归属一个明确工作区，不提供 Gateway scope。未指定工作区时，采用启动配置解析的 home 下默认工作区；Gateway 仅定位、管理后端生命周期和代理请求。
- 将 Goal 构建为内置任务应用，保留目标、预算、主线程归属和现有状态语义；将现有定时会话生成平移为相同组件的内置组合，保留三种会话策略及回报、时区、补跑等行为，不保留独立定时任务模型或执行分支。
- 删除 `GoalRuntimeService` 及其容器装配、独立事件订阅、关闭逻辑和 `resume_active_goals()` 启动 hook，不保留转调新运行时的包装层。工作区启动只等待通用运行时基础设施和受管理 worker 就绪；逐项恢复、判断和续跑由该 worker 异步处理，进度与失败显式可见。
- **BREAKING**：现有 Gateway 生成器的定义、调度和运行协调迁至所属工作区；完成一次性迁移和全部调用方平移后物理删除旧权威存储、调度器及接口，不保留双读双写或两套续跑机制。
- 在主窗口底部面板“自动化”中提供任务列表、详情、模板及组件配置、运行记录和管理操作；右侧侧边栏保留 Goal 专用视图，并展示当前会话的关联任务，复用同一后端事实。

## Capabilities

### New Capabilities

- `workspace-persistent-tasks`: 工作区级任务身份与状态、组件组合、脚本和模型条件判断、可靠投递、恢复、Goal 应用及单链路迁移。
- `persistent-task-management`: 当前工作区的任务管理、模板与高级组件编辑、运行可观察性、会话关联视图和前端状态一致性。

### Modified Capabilities

- `session-generators`: 将生成器的权威定义与调度迁入工作区持久任务运行时，保留现有会话策略、稳定定位器校验、幂等和离线可观察行为。

## Impact

- 工作区业务服务、生命周期装配、Job 事件消费、Goal 服务与工具、Session Generation、工作区 API 和协议类型。
- Gateway 当前 `control/generators.py`、`control/scheduler.py`、`control/coordinator.py` 及其 API、配置消费者和前端调用方；实施前必须全仓盘点直接及间接消费者。
- `src/clients/web` 的底部面板自动化、Goal 视图和会话关联视图；现阶段不扩展原生客户端。
- 复用 `add-context-injection-lifecycle` 与 `add-itemized-rollout-context` 的唯一上下文 mutation owner、source 生命周期、内部执行准入及 durable wakeup；不建立第二套上下文或 thread 执行器。
- 与 `add-workspace-persistent-resource-management` 的外部资源 owner 区分：持久任务可以引用资源，不接管 Terminal、Browser 等资源生命周期；稳定资源寻址沿用统一资源 URI 合同。
