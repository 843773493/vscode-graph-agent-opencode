# 目录用途

`configs/` 存放配置模板、JSON Schema、用户配置生成器和仅供测试使用的配置样例。

# 可修改内容

- 可以维护 `configs.boxteam` 配置生成入口、基础模板和配置 schema。
- 可以维护 `configs/tests/` 下由 E2E fixture 复制到隔离工作区的测试配置。
- 可以通过显式开发安装参数选择性加入测试工具、Gateway 测试工作区等开发配置。

# 不可修改内容

- 不要把 API key、私钥内容、用户绝对路径或运行时状态写入仓库模板。
- 不要在模型配置中擅自增加 `max_tokens`、`max_output_tokens`、采样参数或 reasoning 覆盖。
- 不要让测试配置在运行时直接修改用户全局配置或 `asset/` 模板。
- 不要恢复旧的用户配置路径 `~/.boxteam/boxteam.jsonc`。

# 规范

- 用户级配置输出到 `${BOXTEAM_HOME:-~/.boxteams}/config/boxteam.jsonc`，schema 与配置文件放在同一目录。
- 工作区级配置固定为 `${workspace_abs_path}/.boxteam/boxteam.jsonc`，并覆盖用户级同名配置项。
- 使用 `uv run python -m configs.boxteam` 调用配置生成器，不从源码文件位置向上推导项目根目录。
- 默认生成最小模型配置；只有用户明确要求或官方接口验证为必需时才添加请求参数覆盖。
- 生成器写入前必须执行既定迁移；配置安装属于显式的整文件重建，必须使用原子替换并在失败时给出明确的源、目标和处理建议。
