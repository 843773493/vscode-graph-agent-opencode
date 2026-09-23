# 目录用途

`configs/tests/` 汇总配置相关的测试配置，作为 `configs/tests/model_stream/` 与 `configs/tests/workspace/` 的父目录。它不参与发行包内置默认配置，只服务于测试选择测试 Agent、Provider、工具策略与 Workspace 行为。

# 可修改内容

- 可以新增或调整测试配置子目录及其场景文件，并同步补齐每个新子目录的 `AGENTS.md`。
- 可以维护 `model_stream/` 下的 transport/scenario 选择配置与 `workspace/` 下的完整 Workspace 测试配置。

# 不可修改内容

- 不存放或修改发行包内置默认配置：内置默认固定为仓库根 `configs/gateway_inline.jsonc` 与 `configs/workspace_inline.jsonc`，本目录不得充当产品运行时默认配置。
- 不写入 API key、Authorization、Cookie、私钥或个人机器绝对路径；凭据只能通过环境变量引用。
- 不把测试配置产物写入仓库根、`src/` 或 `configs/` 顶层。

# 规范

- 子目录各自通过同目录 schema 校验：`model_stream/` 用 `model_stream_schema.jsonc`，`workspace/` 用 `../../workspace_schema.jsonc`。
- 文件名使用稳定的测试场景名并保留 `.jsonc` 格式。
- 测试工作区与临时产物必须落到 `out/tests/` 对应隔离运行目录，不修改用户全局配置或 `asset/` 模板。
- 新增子目录前先明确职责，并补齐对应 `AGENTS.md`。
