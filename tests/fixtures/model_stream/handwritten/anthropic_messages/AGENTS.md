# 目录用途

存放手写 Anthropic Messages SSE 的原始 provider cassette。

## 可修改内容

- Anthropic Messages 的标准 `message_*`、`content_block_*` SSE frame。
- 与 frame 一一对应的请求匹配字段和测试模型标识。

## 不可修改内容

- 不写入 LangChain `AIMessage`、消息流事件或前端展示字段。
- 不把 Chat Completions 或 Responses 的 carrier 塞入 Anthropic frame。

## 规范

- 每个 cassette 只描述 Anthropic Messages 协议。
- 思考、文本和工具调用必须使用 Anthropic 原生 content block 事件表示。
- 修改后必须运行 model stream asset loader 和对应 provider 测试。
