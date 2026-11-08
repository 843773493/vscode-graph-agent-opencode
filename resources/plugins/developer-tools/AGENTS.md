# 目录用途

维护 BoxTeam 内置 developer-tools 插件包清单及其锁定 Node 依赖声明。

# 可修改内容

- 可以维护 `plugin.jsonc`、`package.json` 和对应 npm lock。
- 可以修改与包身份、入口或依赖解析直接相关的元数据。

# 不可修改内容

- 不在这里复制 Terminal 产品源码；唯一源码位于 `src/workspace-services/terminal/`、`src/shared/` 和 `src/workspace-services/protocol/`。
- 不提交 `node_modules`、已编译原生依赖或 ZIP 构建产物。
- 不在插件包中实现第二份 Terminal 业务或协议实现。

# 规范

- 包身份固定为 `boxteam.developer-tools@0.1.0`，包内 `terminal` 插件固定为 workspace scope。
- 依赖以 `package-lock.json` 为准；发布 ZIP 由 `scripts/plugins/pack-developer-tools.mjs` 从仓库根目录组装。
- 包源码路径保持与仓库源目录相同，打包显式文件清单由真实标准插件 ZIP 启动验收验证入口可运行。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
