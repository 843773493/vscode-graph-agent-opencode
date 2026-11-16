# 目录用途

拥有单次 Agent 执行中的消息流运行时，把模型 block、工具调用和活动提交给既有 writer 与 canonical 端口。

- `runtime.py`：`MessageStreamRuntime` 的唯一装配、共享状态与规范化 block 观察端口。
- `block_assembly.py`：provider chunk 归属、规范化 block 与工具参数累计。
- `tool_calls.py`：工具调用、真实 attempt、结果和待提交完成事件的关联。
- `finalization.py`：模型完成、重试、中断和消息流终态；不管理 PTY 或终端进程。
- `activities.py`：消息流活动处理器、活动生命周期与当前执行中的活动访问。
- `trace_observer.py`：已提交规范化 block 到 Trace 诊断事件的投影。

# 可修改内容

- 可以维护上述运行时内部状态、组合类和写入端口的调用顺序。
- 调用方直接导入实际 owner，运行时入口是 `runtime.MessageStreamRuntime`。

# 不可修改内容

- 不实现 JSONL、SQLite 或 checkpoint 持久化；对应事实仍归原基础设施 owner。
- 不承担 `event_stream/` 的 Agent 事件读取、身份校验或 watchdog，也不承担 `execution_step/` 的执行重试决策。
- 不保留旧根模块转导出或另建消息正文、工具结果事实源。

# 规范

- 共享状态只在 Runtime 构造器初始化；组合类按 block、工具调用和终态划分，不能作为独立 service 装配。
- block 观察统一经 Runtime 的 `_observe_normalized_block`；Trace observer 只做诊断投影，不拥有 Runtime 状态。
- 修改命名或目录时迁齐生产、测试及动态模块引用，保留原消息、工具身份与提交顺序。
