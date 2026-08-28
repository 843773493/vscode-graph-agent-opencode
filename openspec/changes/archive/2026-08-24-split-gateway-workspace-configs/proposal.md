## Why

Gateway 控制面配置与 Workspace 业务配置当前共享同一个 schema、默认值生成器和持久化文件，导致 Gateway 需要经过工作区配置加载器，源码开发还存在 Python 默认值与 overlay 合并路径。需要按运行时所有权拆分配置，使源码开发、源码安装和 npm 安装都只加载 `${BOXTEAM_HOME}` 中的静态 JSONC 配置。

## What Changes

- **BREAKING**：用户级 `boxteam.jsonc` 拆分为 `gateway.jsonc` 与 `workspace.jsonc`，工作区级 `boxteam.jsonc` 改名为 `workspace.jsonc`。
- 新增独立的 Gateway、Workspace JSON Schema 和普通/开发静态 JSONC 模板，所有实际配置统一使用 `.jsonc`。
- Gateway 只加载 `${BOXTEAM_HOME}/config/gateway.jsonc`；Workspace Backend 加载用户级 `workspace.jsonc`，再合并显式工作区的 `.boxteam/workspace.jsonc`。
- 删除 `configs/defaults.py`、`configs/development.overlay.jsonc` 以及运行时配置动态拼装逻辑。
- 源码开发启动在启动服务前把 `gateway_dev.jsonc`、`workspace_dev.jsonc` 和源码 `.env` 原子安装到 development `BOXTEAM_HOME`；运行时不读取源码模板。
- 将开发 SSH 资产安装与配置选择解耦，开发容器注册信息由 `gateway_dev.jsonc` 声明。
- 增加旧合并配置到双配置布局的一次性、可恢复迁移；迁移完成后不保留旧路径运行时 fallback。

## Capabilities

### New Capabilities

- `configuration-domains`: 定义 Gateway 控制面与 Workspace 业务配置的文件所有权、加载边界、独立 schema/version 和旧布局迁移。

### Modified Capabilities

- `configuration-bootstrap`: 初始化与开发安装从单个动态生成配置改为两个静态 JSONC 配置及各自 schema。
- `runtime-launcher`: Launcher 启动前安装或迁移双配置文件，并让所有发行方式只从 `BOXTEAM_HOME` 加载。
- `packaged-runtime-distribution`: npm runtime 打包并安装四个静态配置资源，而不是调用 Python 默认值生成器。

## Impact

- 影响 `configs/`、`scripts/dev.mjs`、Launcher、Gateway 配置依赖、Workspace `ConfigService`、配置 CLI、路径工具和存储迁移。
- 影响 npm runtime 资源清单与 staging、Gateway/Workspace E2E fixture、配置迁移及热重载测试。
- 用户配置文件名和 schema 引用发生破坏性变化，需要在服务启动前完成布局迁移。
