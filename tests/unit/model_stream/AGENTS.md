# 目录用途

存放 `app/testing/model_stream` 的模型 stream 资产、匹配、配置和并发回放单元测试。

## 可修改内容

- 配置加载、cassette 校验、SSE frame 和 replay policy 的单元测试。
- 不依赖真实 provider 的 HTTPX transport 测试。

## 不可修改内容

- 不请求真实模型，不测试完整 Agent 或业务服务链路。
- 不把业务事件协议断言混入 provider cassette loader 测试。

## 规范

- 文件系统使用 pytest fixture 隔离。
- 并发测试必须断言每个 response 的完整 frame 序列和共享状态隔离。
