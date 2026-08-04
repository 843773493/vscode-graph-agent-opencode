# 目录用途

存放 Agent 执行、模型 Provider、上下文、工具调用和多 Agent 协作的端到端测试。

# 可修改内容

- Agent 公开 API 和真实执行链的 E2E 用例。
- Agent 模型、工具、附件、上下文与协作行为断言。

# 不可修改内容

- 不放纯 Session CRUD、Job 调度或存储迁移测试。
- 不使用前端本地状态代替后端真实结果。

# 规范

- 通过真实 HTTP、SSE 和工作区文件验证 Agent 外部行为。
- 模型结果断言应使用稳定 marker，不依赖无关文案。
- 正式产物写入与本目录测试路径对应的 `out/tests/e2e/backend/agents/`。
