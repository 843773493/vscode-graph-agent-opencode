# 目录用途

存放 Workspace 后端系统的严格端到端测试，包括 Agent 执行、真实模型 Provider、Job、Session 和持久化子分区。

# 可修改内容

- Agent 公开 API 和真实执行链的 E2E 用例。
- Agent 模型、工具、附件、上下文与协作行为断言。

# 不可修改内容

- 不使用前端本地状态代替后端真实结果。
- 不使用模型 stub 或进程内 fake 代替真实执行链。

# 规范

- 通过真实 HTTP、SSE 和工作区文件验证 Agent 外部行为。
- 模型结果断言应使用稳定 marker，不依赖无关文案。
- 正式产物写入与本目录测试路径对应的 `out/tests/e2e/system/agent/`。
