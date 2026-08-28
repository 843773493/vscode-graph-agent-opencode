## Why

持久化配置当前直接保存内置工具的 Python 工厂路径，源码重构后旧用户配置与工作区覆盖会在 Agent 执行阶段才以 `AttributeError` 失败。软件需要在配置进入运行时前完成可审计、可重复的版本迁移和工具解析预检，使旧会话能够使用当前配置安全重试。

## What Changes

- 为用户级和工作区级配置增加独立的 `config_version`，并按文件逐版本迁移后再合并。
- 内置工具配置改用稳定 `tool_id`；Python 工厂路径仅作为自定义扩展边界保留。
- 增加旧会话历史工具到 `read_context`、`search_context` 的迁移规则，并原子写回配置。
- 在启动和热重载提交配置快照前预检工具 ID、工厂导入和重复项，失败时报告具体文件与配置位置。
- 增加配置迁移/诊断 CLI，并覆盖用户配置、工作区覆盖、热重载与旧会话重试测试。
- **BREAKING**：新生成的内置工具配置不再暴露或依赖 Python 工厂路径。

## Capabilities

### New Capabilities

- `configuration-evolution`: 版本化配置迁移、稳定内置工具 ID、配置预检及迁移诊断命令。

### Modified Capabilities

- `configuration-bootstrap`: 初始化配置必须生成当前 `config_version` 和稳定内置工具声明，并能升级已有用户配置。

## Impact

- 影响 `configs/` 配置生成器与 schema、`ConfigService` 加载/热重载管线、Agent 自定义工具解析和配置 CLI。
- 用户级 `${BOXTEAM_HOME}/config/boxteam.jsonc` 与每个 `${workspace}/.boxteam/boxteam.jsonc` 将分别迁移。
- Gateway 只负责工作区生命周期与状态展示；工作区配置迁移仍由对应工作区后端执行。
