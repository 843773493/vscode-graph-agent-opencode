# tests/unit/agents/tools

## 目录用途

存放 `app/agents/tools/` 内置 Agent 工具及其运行时协议的单元测试。

## 可修改内容

- 内置工具 schema、参数校验、执行行为和错误传播测试。
- 工具测试所需的 fake 客户端与 fixture。

## 不可修改内容

- 不调用真实外部服务或用户工作区资源。
- 不在此测试 Agent 中间件或 Provider 行为。

## 规范

- 工具执行必须使用临时工作区并断言真实结果。
- 注入的运行时参数不得暴露到模型可见 schema。
