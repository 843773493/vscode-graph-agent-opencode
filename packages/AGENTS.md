# 目录用途

`packages/` 存放可独立构建、测试或发布的 BoxTeam JavaScript 包。

# 可修改内容

- 可以新增 Launcher、平台适配和其他具有明确 package 边界的 ESM 模块。

# 不可修改内容

- 不在这里复制 Python Agent 业务逻辑或工作区持久化规则。
- 不提交 npm tarball、node_modules 或平台二进制构建产物。

# 规范

- 包管理、安装和测试统一使用 Bun；发布兼容性测试可以显式调用 npm。
- 每个子包必须声明自己的资源边界和 AGENTS.md。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
