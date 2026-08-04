# tests/unit/agents/providers

## 目录用途

存放 `app/agents/providers/` 模型 Provider 适配器和协议格式的单元测试。

## 可修改内容

- 请求构造、流式响应、推理块和 Provider 能力行为测试。
- 明确隔离的 Provider fake 与响应样例。

## 不可修改内容

- 默认不得请求真实模型 Provider。
- 不在此测试通用 Agent 工具或业务服务。

## 规范

- 上游请求必须通过 mock 或显式标记的受控 fixture 隔离。
- 测试需断言标准 LangChain 消息格式和错误传播。
