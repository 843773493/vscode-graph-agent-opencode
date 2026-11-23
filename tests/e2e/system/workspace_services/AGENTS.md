# 目录用途

存放 Browser、Terminal 与 Web Search 真实工作区辅助服务链路的 E2E。

## 可修改内容

- 真实辅助服务进程、真实协议和真实工具调用测试。

## 不可修改内容

- 不得伪造被验收辅助服务的工具结果。模型可使用真实调用、上游重放或手工 SSE；外部输入和替换边界须明确，不以模型模式决定目录归属。

## 规范

- 自包含 `data:` 页面可以作为输入数据；替代真实外部服务的 HTTP server 不可以留在 E2E。
- 子目录使用合法 Python 包名，公共 helper 放入 `tests/harness/` 或 `tests/support/`。
