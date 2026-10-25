# 目录用途

存放通过录制的 Provider stream 响应驱动真实 Agent 与后端执行链的端到端用例。

# 可修改内容

- Chat Completions 与 Responses stream 在真实业务链中的结果断言。
- 场景回放配置和供人工复查的 E2E review artifact。

# 不可修改内容

- 不得用进程内 Agent、Job 或 Session fake 替代真实后端执行链。
- 不得修改生产 Provider 协议以适配单个回放资产；资产和 codec 必须保持协议一致。

# 规范

- 文件名直接说明协议和场景，目录已表达 `model_stream`，不再重复此前缀。
- 每个模块用 `MODEL_STREAM_CONFIG_NAME` 声明场景配置名，由本目录 `conftest.py` 统一解析路径。
- 回放资产从 `tests/fixtures/model_stream/` 加载。
- 产物通过 `output_root_for_test(Path(__file__), test_layer="e2e")` 写入镜像测试路径。
