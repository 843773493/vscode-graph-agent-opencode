# 目录用途

存放 model-stream 测试的 transport、scenario 选择和产物路径配置；这些文件控制测试如何运行，不保存 Provider 上游响应资产。

# 可修改内容

- 可以维护 `off`、`record`、`replay` 模式及 scenario 选择配置。
- 可以维护本目录共享的 `model_stream_schema.jsonc`。

# 不可修改内容

- 不存放 cassette、Provider frame 或业务 expectation；长期资产属于 `tests/fixtures/model_stream/`。
- 不写入 API key、Authorization、Cookie 或真实上游响应中的敏感信息。
- 不把配置产物写入仓库根目录、`src/` 或 `configs/` 顶层。

# 规范

- 每个 model-stream 配置必须通过同目录的 `model_stream_schema.jsonc` 校验。
- `fixture_root` 可省略；省略时由 loader 使用 `tests/fixtures/model_stream/`，只有独立 fixture 集合才显式覆盖。
- 临时录制和诊断产物必须落到 `out/tests/` 或 `out/tests/temp/` 对应路径。
