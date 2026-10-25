# tests/unit/agents/providers

## 目录用途

存放 `app/agents/providers/` 模型 Provider 适配器和协议格式的单元测试。

## 测试职责树

```text
providers/
├── test_chat_provider_request_configuration.py  provider 配置到 Chat 请求参数的投影
├── test_chat_provider_stream_lifecycle.py       Chat 流式 delta、取消、重试与错误归属
├── test_responses_provider_lifecycle.py          Responses payload 与事件流生命周期
├── test_responses_history_projection.py          Responses 历史工具调用归一化与回放
├── test_provider_format_check.py                LangChain 消息块与内容格式合同
├── test_provider_api_mode.py                     Provider API mode 能力合同
├── test_request_validation.py                    Provider 请求边界校验
└── test_<provider>.py                            单一 Provider 的专用协议合同
```

同一文件只覆盖一条 Provider 行为链；配置投影与流式生命周期分开，Chat Completions 和 Responses 分开。

## 可修改内容

- 请求构造、流式响应、推理块和 Provider 能力行为测试。
- 明确隔离的 Provider fake 与响应样例。

## 不可修改内容

- 默认不得请求真实模型 Provider。
- 不在此测试通用 Agent 工具或业务服务。

## 规范

- 上游请求必须通过 mock 或显式标记的受控 fixture 隔离。
- 测试需断言标准 LangChain 消息格式和错误传播。
