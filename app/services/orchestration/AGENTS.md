# 目录用途

`app/services/orchestration/` 放流程编排与运行控制相关实现，负责串联业务服务、控制自动继续、调度执行流程。

## 职责树与主流程

```text
orchestration/
├── job_execution_service.py             Job 状态转换、标题触发、step 调用及完成/失败事件
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
├── message_stream_runtime.py             将消息流状态组合到单一 writer/runtime
├── stream_block_assembler.py             流式 block 组装
├── tool_call_registry.py                 工具调用与结果的关联、去重和等待
├── activity_runtime.py                   活动处理器注册和工具活动发布
├── terminal_finalization.py              消息流终态和中断事实收口
├── trace_observer.py                     消息流 trace 观察接线
├── goal_runtime_service.py               Job 事件驱动的 Goal 捕获、续跑和终止状态
├── initial_execution_binding_worker.py   持久初始执行 intent 的 claim/bind 消费
├── thread_initial_execution_service.py   child Thread 初始执行绑定与 dispatch
├── owner_thread_creation_factory.py      按操作作用域装配 Thread 创建依赖并显式关闭控制连接
├── runtime_source_dispatcher.py           内部 runtime source/wakeup 的准入与恢复派发
├── mcp_wait/                              MCP 连接终态观察、原子通知与原 thread 独立执行投递
├── thread_residency.py                    runtime admission、generation、blocker 与 idle 回收流程
├── thread_residency_state.py               驻留状态、runtime slot、lease 和快照的数据形状
├── session_subagent_service.py            Session subagent 委派
├── session_title_service.py               首条用户消息的标题生成与规范化
├── terminal_steering_service.py            Terminal 完成后的执行引导
├── workspace_observation.py               工作区观察流接线
├── agent_stream_helpers.py                 Agent 流事件与工具结果的纯转换
├── communication/                         Session 间 Inbox admission 与冷观察
├── resource_activation/                    Execution/ModelCall 资源快照冻结
└── team/                                   团队 fanout、恢复与 team_state 发布
```

主执行链为 `JobExecutionService → AgentExecutionService → execution_step.StepRunner → event_stream → MessageStreamRuntime/既有写入端口`。`GoalRuntimeService` 是独立的 Job 生命周期消费者：`JOB_STARTED` 在后台捕获 Goal 归属，`AGENT_END` 结算并派发续跑，`JOB_COMPLETED` 确保活动 Goal 在空闲时启动；`JOB_FAILED` 与 `JOB_CANCELLED` 分别收敛为 blocked 与 paused。查找执行重试、失败收口或事件身份时进入对应子目录，不要在公共 service 或事件消费处复制同类规则。

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
- 保持装配边界清晰，依赖协议而不是具体实现。
- 失败必须立即暴露，不能默认继续。
