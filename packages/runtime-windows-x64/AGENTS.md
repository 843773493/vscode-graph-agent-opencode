# 目录用途

`packages/runtime-windows-x64/` 是 Windows x64 自包含运行时 npm 平台包的元数据模板目录。

# 可修改内容

- 可以维护 Windows x64 平台包的 `package.json`、资源导出和版本元数据。
- 可以调整与 `packaging/runtime/` 构建脚本一致的包边界。

# 不可修改内容

- 不得提交构建生成的 Python、Chromium、应用文件、`node_modules` 或 npm tarball。
- 不得在此目录复制 Launcher、Agent 业务逻辑或工作区数据。

# 规范

- 实际运行时内容只能由 `bun run package:windows-x64` 生成到 `out/packaging/windows-x64/`。
- 包必须声明 `win32` 和 `x64` 的 npm 平台约束，并导出相对路径 runtime manifest。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
