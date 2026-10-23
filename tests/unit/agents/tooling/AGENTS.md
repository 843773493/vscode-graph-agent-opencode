# tests/unit/agents/tooling

## 目录用途

存放 `app/agents/custom_tools.py` 与 `app/agents/model_tool_schema.py` 的单元测试，分别验证自定义工具配置装配和模型可见工具 schema。

## 可修改内容

- 工具配置项解析、规范化、去重和 factory 参数传递测试。
- 模型可见 schema 投影、公开参数校验与协议工具 schema 测试。
- 上述测试专用的配置样例和 fake factory。

## 不可修改内容

- 不在此测试内置工具执行；该职责归 `tools/`。
- 不在此测试工具确认或事件中间件；这些测试留在 `tests/unit/agents/` 根职责组。
- 不调用真实 Provider、网络或用户工作区。

## 规范

- 模型 schema 不得暴露运行时注入参数；同时验证 schema 投影和参数校验边界。
- 配置解析测试要断言规范化后的配置及错误传播，不以默认值掩盖无效配置。
