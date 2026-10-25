# 目录用途

存放 Workspace 后端系统的严格端到端测试，包括 Agent 执行、真实模型 Provider、Job、Session 和持久化子分区。

## 职责树

```text
agent/
├── model_stream/       从 Provider 边界回放录制响应，覆盖真实 Agent 业务执行链
├── context/            上下文与 Provider provenance
├── jobs/               Job 调度与 Session 中断
├── sessions/           Session 生命周期、Goal 与资源
├── storage/            日志、checkpoint 与 session catalog 持久化
└── test_*.py           Agent 能力、工具和跨域端到端场景
```

目录按被验证的业务边界分组；协议回放资产位于 `tests/fixtures/model_stream/`，正式产物仍按测试文件路径写入 `out/tests/e2e/system/agent/`。

# 可修改内容

- Agent 公开 API 和真实执行链的 E2E 用例。
- Agent 模型、工具、附件、上下文与协作行为断言。

# 不可修改内容

- 不使用前端本地状态代替后端真实结果。
- 不使用模型 stub 或进程内 fake 代替 Agent 与后端业务执行链。
- 模型 replay 只能替代 Provider 请求边界；工作区服务和 Agent 调用链仍须经真实应用运行。

# 规范

- 通过真实 HTTP、SSE 和工作区文件验证 Agent 外部行为。
- 模型结果断言应使用稳定 marker，不依赖无关文案。
- 正式产物写入与本目录测试路径对应的 `out/tests/e2e/system/agent/`。
