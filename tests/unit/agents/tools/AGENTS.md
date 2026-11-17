# tests/unit/agents/tools

## 目录用途

存放 `app/agents/tools/` 内置 Agent 工具及其运行时协议的单元测试。

`test_extension_invocation.py` 覆盖固定扩展入口的 schema、分派、结果配对与调用上下文；`test_extension_invocation_binding.py` 覆盖封存目录绑定、注册身份与执行点撤权；参数错误归 `test_extension_argument_errors.py`。

`test_tool_discovery.py` 只校验工具发现输入的选择器与预算；真实发布目录和 Saver 恢复链路归 `tests/integration/backend/sessions/tool_discovery/`。

## 可修改内容

- 内置工具 schema、参数校验、执行行为和错误传播测试。
- 工具测试所需的 fake 客户端与 fixture。

## 不可修改内容

- 不调用真实外部服务或用户工作区资源。
- 不在此测试 Agent 中间件或 Provider 行为。

## 规范

- 工具执行必须使用临时工作区并断言真实结果。
- 注入的运行时参数不得暴露到模型可见 schema。
