# 目录用途

`app/services/orchestration/` 放流程编排与运行控制相关实现，负责串联业务服务、控制自动继续、调度执行流程。

## 职责树与主流程

```text
orchestration/
├── background_work/                     精确 Thread 后台工具任务、持久邮箱与恢复；不管理工作区自动化定义
├── session_messages.py                  SessionMessageOrchestrator：用户消息准入、幂等接收、Job 派发与内部消息投递
├── execution_queue.py                    ExecutionQueueService：聚合 rollout 队列、领取与启动持有权、待处理投影；不另存队列
├── job_execution_service.py             Job 状态转换、标题触发、step 调用及完成事件；失败归 JobService 结算
├── agent_execution_service.py           Agent/runtime 组装与复用；把单次执行交给 StepRunner
├── execution_step/                       单次 Agent step 的控制循环、重试、终态与端口接线
│   ├── ports.py                          step 所需的显式依赖与 Agent factory
│   ├── runner.py                         单次执行、事件消费及清理顺序
│   ├── retry.py / reminders.py           重试决策与继续执行提示
│   ├── failures.py                       取消和失败收口
│   └── model_call.py / stream_bindings.py ModelCall 与事件发布端口绑定
├── event_stream/                         Agent 事件身份、消费、结果与工具/模型事件解释
│   ├── contracts.py / identity.py        事件源契约与 run/session/job 身份校验
│   ├── reader.py                          事件读取、超时和取消处理
│   ├── processor.py                       按事件驱动运行时状态与现有写入端口
│   └── model_events.py / tool_events.py   模型用量和工具展示数据
├── message_stream/                       消息流运行时；模型 block、工具调用、活动和终态收口
│   ├── runtime.py                        MessageStreamRuntime 状态与端口装配
│   ├── block_assembly.py / tool_calls.py  provider block 组装与工具调用、结果关联
│   ├── activities.py / finalization.py    活动发布与消息流终态（不管理终端进程）
│   └── trace_observer.py                 规范化 block 到 Trace 诊断事件的投影
├── initial_execution_binding_worker.py   持久初始执行 intent 的 claim/bind 消费
├── thread_initial_execution_service.py   child Thread 初始执行绑定与 dispatch
├── owner_thread_creation_factory.py      按操作作用域装配 Thread 创建依赖并显式关闭控制连接
├── runtime_source_dispatcher.py           内部 runtime source/wakeup 的准入与恢复派发
├── mcp_wait/                              MCP 连接终态观察、原子通知与原 thread 独立执行投递
├── thread_residency.py                    runtime admission、generation、blocker 与 idle 回收流程
├── thread_residency_state.py               驻留状态、runtime slot、lease 和快照的数据形状
├── session_subagent_service.py            Session subagent 委派
├── session_title_service.py               首条用户消息的标题生成与规范化
├── session_turn_replay_service.py         按 Message/Turn 定位、回退上下文并重新派发
├── terminal_steering_service.py            Terminal 完成后的执行引导
├── workspace_runtime_lifecycle.py         工作区执行启动恢复、Job 准入排空和强制中断
├── communication/                         Session 间 Inbox admission 与冷观察
├── resource_activation/                    Execution/ModelCall 资源快照冻结
└── team/                                   团队 fanout、恢复与 team_state 发布
```

`persistent_tasks/`负责通用任务 worker、手动触发和既有 source 动作对账；定义与运行事实归工作区状态库，不另建执行器。Goal 和生成器的完整迁移仍由对应 OpenSpec 追踪。

主执行链为 `JobExecutionService → AgentExecutionService → execution_step.StepRunner → event_stream → MessageStreamRuntime/既有写入端口`。`persistent_tasks/` 的同一受管理 worker 消费 Job 生命周期并恢复 Goal：`JOB_STARTED` 在同步发布边界冻结 Goal 归属，`AGENT_END` 结算并派发续跑，`JOB_COMPLETED` 补偿空闲激活；失败与取消分别收敛为 blocked 与 paused。关闭时先停止动作准入，排空在途 Job 终态后再停止 worker。查找执行重试、失败收口或事件身份时进入对应子目录，不要在公共 service 或事件消费处复制同类规则。

## 可修改内容

- 可以新增、修改、删除编排服务实现文件。
- 可以维护 job / session / agent 之间的执行顺序与控制逻辑。
- `team/`编排Session协作fanout、冷投递恢复和Registry发布，消费既有ledger及Thread pending owner，不另持协作事实。

## 不可修改内容

- 不要把纯业务状态计算放进这里。
- 不要把基础设施实现细节放进这里。
- 不要把映射转换逻辑放进这里。

## 规范

- 以流程控制为中心，不承担数据持久化职责。
- `execution_queue.py` 的全工作区恢复扫描逐个取得短 topology 准入和 Session read guard，重新核对 catalog active 状态后读取 main/child 队列；guard 覆盖同步读取线程及取消结算，不并持两个 Session 锁。删除排空中的精确 Session 读取沿用调用方已有准入，不重入该锁；原存储故障继续抛出。执行数据由 rollout owner 保存，既有 v5 显式迁移仍经同一 owner 导入。
- 保持装配边界清晰，依赖协议而不是具体实现。
- `AgentExecutionService.list_tool_definitions` 装配并缓存不含执行状态的管理目录，`_tool_catalog_cache` 按 Agent、配置及 MCP 版本失效；纯字段转换归 `mapping/tool_catalog.py`，目录查询不构建模型或执行图。
- 失败必须立即暴露，不能默认继续。
- `workspace_runtime_lifecycle.py` 协调工作区执行，不管理 Backend 进程或 SDK 装配。启动恢复在受保护 worker 中耗尽 SQLite Trace 快照，只保留 Job owner 和生命周期 ID；全部历史格式、协议与跨会话归属检查通过后才领取队列、调用恢复端口或写入终态，不用尾部窗口代替完整校验，不另建恢复账本。
- 工具参数流只在累计正文可能以 JSON 对象结束时调用标准 JSON 解析器；不完整、格式无效或非对象的输入仍保留 raw 和未完成标记，不能提前触发工具执行。
- Terminal完成监控的关闭每次最多观察原任务30秒；到期保留句柄和关闭准入，重入不再次取消原扫描或重启监控。正常轮询中的可重试错误在关闭阶段须交回原异常，不能消耗后返回成功。原结果交回Workspace后才继续后续依赖，不能把观察到期当作扫描已停止或整个shutdown已有硬期限。
- Terminal完成监控以插件状态SSE驱动，保留变化资源ID并只查询所选摘要；初始／重连和每60秒沿原owner完整对账孤儿期限及漏通知，连续局部事件不能推迟周期对账。心跳和PTY输出不触发扫描；插件流断开按5秒重连，不重放管理动作；一个工作区只由原监控持有一条插件状态流。
- Agent驻留服务由唯一关闭任务等待原扫描及tracker排空，每次调用最多观察30秒；观察取消或到期保留原任务和关闭准入，扫描结算前不清句柄或缓存。关闭期原错不按周期重试消耗，迟到失败先交回观察者，再允许显式重试剩余关闭；不能重启扫描或另建资源事实源。
