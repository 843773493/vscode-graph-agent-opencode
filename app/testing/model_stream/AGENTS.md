# 目录用途

提供测试用模型上游 stream asset 的加载、SSE frame 编解码、请求匹配、并发回放和录制 transport。

## 可修改内容

- `model_stream_cassette` 数据模型、scenario resolver、strict matcher 和 replay policy。
- LiteLLM 使用的异步 HTTP session 测试注入，以及安全的录制产物写入。

## 不可修改内容

- 不改写 provider endpoint，不实现监听端口的模型服务。
- 不生成 SDK chunk、block 或业务事件；这些必须由真实业务链路产生。
- 不保存 API key、Authorization 或未脱敏长期 transcript。

## 规范

- 共享的 cassette 数据必须只读；每个请求独立创建 response stream 和 cursor。
- 只在 `BOXTEAM_TEST_MODEL_STREAM_CONFIG` 显式配置后安装 transport。
- 配置、资产和请求不匹配都必须显式抛出详细错误。
