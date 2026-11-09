# 目录用途

存放 Node Debug owner、插件运行通道、Inspector 与会话存储的单元测试。

```text
node_debug/
├── test_node_debug_runtime_config.py        调试运行配置读取与能力投影
├── test_node_debug_configuration_*.py       调试配置构造与控制
├── test_node_debug_breakpoint*.py            断点表达式、生命周期和源码映射
├── test_node_debug_inspector_*.py            CDP Inspector 帧、超时和接收失败
├── test_node_debug_launch_claim.py           启动认领与幂等恢复
├── test_node_debug_process_lease.py          插件 run claim 与资源 lease 投影
├── test_node_debug_runtime_observation.py    运行时状态观察
├── test_node_debug_source_reconciliation.py  源码映射重建
├── test_node_debug_exception_message.py      异常消息归一
├── test_node_debug_session_store.py          调试会话持久状态
├── test_node_debug_thread_ownership.py        Thread 所有权与隔离
└── test_node_debug_fork.py                    fork 状态继承
```

# 可修改内容

- 插件 run 与 Backend claim、lease、Inspector 控制消息、断点、源码映射、配置、会话存储及 Thread 所有权测试。

# 不可修改内容

- 不终止当前测试未创建的进程，不把真实用户调试进程接入测试。
- 不测试调试 UI、Gateway 代理流程或 Agent 业务编排。

# 规范

- 依赖通过 pytest fixture 注入；目标进程由 Debug 插件拥有，不在 Backend 单元测试中直接创建。
- 已迁往 Gateway 插件的运行时，不在 Backend 测试中伪造其 PID、Inspector 端口或 OS 进程身份。
- 运行本目录测试时使用进程外超时和内存上限，正式产物按测试文件路径保留在 `out/tests/unit/services/infrastructure/node_debug/`。
