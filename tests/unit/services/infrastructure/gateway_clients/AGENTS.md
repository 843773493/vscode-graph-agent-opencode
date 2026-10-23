# 目录用途

本目录组织 Gateway HTTP 客户端的单元测试。客户端只传输 Gateway 请求与响应，不在此处
实现 Gateway 路由、会话业务规则或工作区状态所有权。

```text
gateway_clients/
├── conftest.py                              # 两类客户端共用的代理环境隔离
├── test_gateway_communication_client.py    # Gateway 与目标 Workspace 的启动就绪核验
├── test_gateway_context_protocol_errors.py # 上下文查询畸形回执的错误与诊断边界
├── test_gateway_fork_protocol_errors.py    # fork 畸形回执的普通错误与原始诊断边界
└── test_gateway_session_context_client.py   # 上下文查询与 fork relay 传输
```

# 可修改内容

- 可以维护通信就绪探测、会话上下文/fork relay 协议及其错误诊断边界测试。
- 可以维护 `conftest.py` 中仅属于这些客户端测试的代理环境 fixture。
- 可以按新增客户端测试扩展本目录，并同步更新本文件的职责树。

# 不可修改内容

- 不得在测试中实现 Gateway 路由、联邦 owner、会话上下文合并或 fork 数据复制逻辑。
- 不得访问真实 Gateway、工作区后端、外网或真实模型服务；HTTP 边界使用显式 stub。
- 不得通过宽松身份、`request_id`、错误状态或生命周期断言来适应被测客户端。

# 规范

- 本目录的伪造 HTTP 测试统一隔离大小写代理环境变量，并 monkeypatch 请求入口。
- 创建的 `httpx.AsyncClient` 必须由测试或 pytest fixture 关闭；不得跨测试共享连接池。
- 测试使用 pytest fixture 注入依赖，运行时用 `uv run pytest` 并加进程外超时与内存保护。
- 测试产物位于 `out/tests/unit/services/infrastructure/gateway_clients/<测试名>/`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
