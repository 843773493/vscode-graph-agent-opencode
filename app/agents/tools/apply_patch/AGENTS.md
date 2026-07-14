# 目录用途

实现 Agent 的 VS Code 风格 `apply_patch` 工具，包括 V4A 补丁解析、工作区文件事务和变更 journal。

# 可修改内容

- V4A patch 的数据模型、解析、上下文匹配与文件执行逻辑。
- `apply_patch` LangChain 工具 schema、结果格式和变更 journal。

# 不可修改内容

- 不在这里实现 Agent runtime 装配、API 路由、前端展示或会话业务。
- 不允许补丁访问当前工作区之外的文件。

# 规范

- 工具参数和 V4A 行为以 `reference_repo/vscode/extensions/copilot` 的 `copilot_applyPatch` 为基准。
- 所有文件操作必须先完成整批解析和校验，失败时不得留下部分修改。
- 工具失败时抛出具体错误，不返回伪成功结果。
