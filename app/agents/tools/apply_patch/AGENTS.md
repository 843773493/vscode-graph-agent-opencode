# 目录用途

实现 Agent 的 VS Code 风格 `apply_patch` 工具，包括 V4A 补丁解析、条件文本发布和变更 journal。

# 可修改内容

- V4A patch 的数据模型、解析、上下文匹配与文件执行逻辑。
- `apply_patch` LangChain 工具 schema、结果格式和变更 journal。

# 不可修改内容

- 不在这里实现 Agent runtime 装配、API 路由、前端展示或会话业务。
- 不允许补丁访问当前工作区之外的文件。

# 规范

- 工具参数和 V4A 行为以 `reference_repo/vscode/extensions/copilot` 的 `copilot_applyPatch` 为基准。
- 所有文件操作必须先完成整批解析、路径能力和调用者预期原字节版本校验；preflight 失败零写入。
- 单文件原子发布，多文件失败停止后续操作，逐路径如实公布部分或未知结果，禁止自动恢复旧内容。
- 异步工具必须保留所属事件循环上的生命周期回调，只把阻塞文件执行交给工作线程。
- 工具失败时抛出具体错误，不返回伪成功结果。
