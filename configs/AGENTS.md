# 目录用途

`configs/` 存放 Gateway/Workspace 静态配置模板、独立 JSON Schema、安装迁移代码和测试配置样例。

# 可修改内容

- 可以维护 `configs.boxteam` 配置安装入口、静态模板和配置 schema。
- 可以维护 `configs/tests/` 下由 E2E fixture 复制到隔离工作区的测试配置。
- 可以维护完整的 `gateway_inline.jsonc`、`workspace_inline.jsonc` 内置默认配置，及 `gateway_dev.jsonc`、`workspace_dev.jsonc` 开发模板。

# 不可修改内容

- 不要把 API key、私钥内容、用户绝对路径或运行时状态写入仓库模板。
- 不要在模型配置中擅自增加 `max_tokens`、`max_output_tokens`、采样参数或 reasoning 覆盖。
- 不要让测试配置在运行时直接修改用户全局配置或 `asset/` 模板。
- 不要恢复旧的用户配置路径 `~/.boxteam/boxteam.jsonc`。

# 规范

- 内置默认配置为仓库/发行包中的 `gateway_inline.jsonc` 与 `workspace_inline.jsonc`；用户级覆盖仍输出到 `${BOXTEAM_HOME:-~/.boxteams}/config/gateway.jsonc` 与 `workspace.jsonc`，各自 schema 与配置文件放在同一目录；同目录的 `gateway_local.jsonc` 与 `workspace_local.jsonc` 是可选的机器私有覆盖，必须被 Git 忽略。
- 工作区级配置固定为 `${workspace_abs_path}/.boxteam/workspace.jsonc`，并覆盖用户级 Workspace 配置同名项；Gateway 不读取工作区配置。
- Gateway 配置合并顺序为 `gateway_inline.jsonc` → 用户级 `gateway.jsonc` → 用户级 `gateway_local.jsonc`；Workspace 配置合并顺序为 `workspace_inline.jsonc` → 用户级 `workspace.jsonc` → 用户级 `workspace_local.jsonc` → 工作区 `.boxteam/workspace.jsonc`；对象递归合并，标量和数组由高优先级层整体覆盖。
- 配置来源必须记录实际加载的文件、层级、优先级和是否存在，并通过配置诊断接口暴露；不得只返回最终合并后的值。
- `gateway_inline.jsonc` 的 `runtime.workspace.default_skill_groups` 只声明发行包内置 Skill 组 ID；Skill 源码统一维护在 `resources/skills/`，不要把说明正文嵌入 Gateway 配置。
- 使用 `uv run python -m configs.boxteam` 调用配置安装器，不从源码文件位置向上推导项目根目录。
- 默认生成最小模型配置；只有用户明确要求或官方接口验证为必需时才添加请求参数覆盖。
- 布局迁移必须在启动服务前完成；配置安装属于显式的整文件重建，必须使用原子替换并在失败时给出明确的源、目标和处理建议。
