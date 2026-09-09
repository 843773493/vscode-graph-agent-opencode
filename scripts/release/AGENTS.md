# 目录用途

`scripts/release/` 存放面向开发者和 CI 的平台发布打包入口。

# 可修改内容

- 可以维护 Linux、Windows 和 Windows 交叉打包命令入口。
- 可以编排 `packaging/runtime/` 中的构建与验证实现。

# 不可修改内容

- 不在这里实现运行时构建细节、平台模板或 Agent 业务逻辑。
- 不把 release asset、tarball、ZIP 或其他构建产物写入源码目录。

# 规范

- JavaScript 脚本使用 ESM，失败时保留底层命令的明确退出信息。
- 所有产物写入 `out/packaging/`，版本和下载资源遵守 `packaging/runtime/` 的固定清单。
- 本地与 CI 使用相同的 `package.json` 发布入口。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
