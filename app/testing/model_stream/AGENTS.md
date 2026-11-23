# 目录用途

提供测试用模型上游 stream asset 的加载、SSE frame 编解码、请求匹配、并发回放和录制 transport。

## 可修改内容

- `model_stream_cassette` 数据模型、scenario resolver、strict matcher 和 replay policy。
- LiteLLM 使用的同步／异步 HTTP transport 测试注入，以及安全的录制产物写入。

## 不可修改内容

- 不改写 provider endpoint，不实现监听端口的模型服务。
- 不生成 SDK chunk、block 或业务事件；这些必须由真实业务链路产生。
- 不保存 API key、Authorization 或未脱敏长期 transcript。

## 规范

- 共享的 cassette 数据必须只读；每个请求独立创建 response stream 和 cursor。
- 只在 `BOXTEAM_TEST_MODEL_STREAM_CONFIG` 显式配置后安装 transport。
- 配置、资产和请求不匹配都必须显式抛出详细错误。
- 同步和异步共用匹配、录制及中断结算；replay 不构造真实出口，record 包装调用方实际选中的代理／直连传输。实例只能关闭自己的真实路由，不关闭共享录放 owner。
- 录放 controller 只持有共享 transport 和匹配状态，不改写 SDK 的 session、client factory 或全局客户端缓存；实际 SDK 调用显式传入模型实例的 client。
