# 目录用途

本目录维护自包含平台运行时的 staging、npm 包组装和 relocation 验证。

# 可修改内容

- 运行时构建脚本、固定资源清单和 npm 平台包模板。
- 运行时结构与体积报告逻辑。

# 不可修改内容

- 不提交下载后的 Python、Chromium、node_modules 或 tarball。
- 不调用 `npm publish`。

# 规范

- JavaScript 始终使用 ESM。
- Python、Chromium和 Node 依赖必须从显式版本清单构建。
- staging 不得引用构建机仓库绝对路径。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
