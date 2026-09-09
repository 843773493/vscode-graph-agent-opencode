# 目录用途

存放完整的 Workspace 测试配置文件，用于为单元、集成和 E2E 测试选择测试 Agent、Provider、工具策略和 Workspace 行为。

# 可修改内容

- 可以维护测试专用的完整 Workspace JSONC 配置。
- 可以维护不包含真实凭据值的 Provider、Agent 和工具策略测试场景。

# 不可修改内容

- 不得写入 API key、Authorization、Cookie、私钥或个人机器路径。
- 不存放 model-stream cassette、scenario 或 expectation；这些资产属于 `tests/fixtures/model_stream/`。
- 不把这里的测试配置作为产品运行时默认配置。

# 规范

- 配置必须使用 `../../workspace_schema.jsonc` 校验最终 Workspace 结构。
- 文件名使用稳定的测试场景名，并保留 `.jsonc` 格式。
- Provider 凭据只能使用环境变量引用；测试工作区必须写入 `out/tests/` 对应隔离运行目录。
