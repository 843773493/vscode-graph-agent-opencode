# 目录用途

`packages/runtime-linux-x64/` 是公开 npm 平台包 `@boxteam/runtime-linux-x64` 的元数据模板。

# 可修改内容

- npm 平台过滤、版本、exports 和许可说明。

# 不可修改内容

- 不在源码目录提交构建生成的 Python、Chromium、应用副本或 tarball。
- 不从此目录直接执行 npm 发布。

# 规范

- 版本必须与 `boxteam` 主包一致。
- 实际包内容由 `packaging/runtime/build-linux-x64.mjs` 写入 `out/packaging/`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
