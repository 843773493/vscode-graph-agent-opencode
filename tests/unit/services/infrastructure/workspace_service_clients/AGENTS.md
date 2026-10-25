# tests/unit/services/infrastructure/workspace_service_clients

## 目录用途

存放 Workspace 后端调用 Browser 和 Terminal 辅助服务的 HTTP client 单元测试。

```text
workspace_service_clients/
├── test_browser_manager_client.py   # Browser 请求、资源 owner 与快照协议
└── test_terminal_manager_client.py  # Terminal 请求、PTY owner 与 transport receipt
```

## 可修改内容

- Browser/Terminal client 的请求头、超时、响应校验和错误映射测试。
- 通过 monkeypatch 与显式响应替身构造的传输测试数据。

## 不可修改内容

- 不启动 Browser 或 Terminal 服务，不访问外部网络。
- 不测试 Gateway 路由或工作区业务状态 owner。

## 规范

- 依赖通过 pytest fixture 注入；HTTP 请求入口必须由测试替身接管。
- 测试保留资源身份、协议版本、错误类别与重试元数据断言。
