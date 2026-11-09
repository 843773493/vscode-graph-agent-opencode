# 目录用途

`app/services/infrastructure/node_debug/process/` 承载 Node Debug 的启动编排、插件控制通道、Inspector/DAP 交互、运行观察、恢复和 claim/资源租约接线。

## 按职责查找

```text
process/
├── launch_claim.py         冻结 Debug 插件绑定与 run 参数，并维护 claim 阶段迁移
├── claim_runtime.py        claim 持久化、状态事件和运行资源租约接线
├── launch_orchestrator.py  配置、claim、Gateway 插件启动及 Inspector 握手编排
├── plugin_runtime.py       精确 Debug 插件实例的 WebSocket 控制通道
├── inspector.py            Inspector 命令、事件及暂停上下文
├── evaluation.py           暂停态表达式求值与有界历史
├── observation.py          插件输出和真实 process.exited 事件归集
└── process_lifecycle.py    通过原 Debug 插件 owner 停止、查询和重接 run
```

# 可修改内容

- 可以维护本子包的 claim、插件控制协议、Inspector、恢复、观察和生命周期实现。
- 可以维护运行时状态中 Debug 插件绑定、目标 PID 展示值、插件事件和 owner 锁的接线。

# 不可修改内容

- 不得在 Backend 用本机 PID 探测、端口猜测或 OS 信号接管/终止目标进程。
- 不得改变 API、Agent 工具、Gateway 插件管理或 Debug 配置的外部职责和合同。
- 不得以预设结果代替插件回执或真实进程事件，也不得吞掉控制通道错误。

# 规范

- 每个 run 冻结唯一的 Gateway、Workspace、插件实例、服务和 registration 绑定；启动后的控制、停止和恢复都使用这个冻结绑定。
- claim 只保存恢复原 Debug run 所需的 owner、绑定和启动快照。PID 可以作为目标状态的展示信息，不是 Backend 的进程控制凭据。
- 正常停止经原插件 `stop/status` 确认；冷恢复只查询并重接同一 run，不重放启动。结果未知要明确保留，不虚报退出。
- `process.exited` 是目标终态的权威观察；Inspector 的结束通知不能伪造退出码。
- 跨子包调用使用明确的绝对模块路径，不反向依赖 `service.py`。
- 修改后运行 `uv run ruff check`、Node 语法检查及相应 Node Debug 专项验证。
