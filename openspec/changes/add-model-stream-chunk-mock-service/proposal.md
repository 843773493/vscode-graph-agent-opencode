## Why

当前模型 stream transport 的 cassette、录制器和回放器默认把上游响应理解为 OpenAI Chat Completions 的 `data: ...` 加 `[DONE]`。这使它无法正确保存或回放 OpenAI Responses 的 `event: response.output_text.delta` 等事件，也无法为已经声明支持的 `anthropic_messages` 留出稳定的协议边界。尤其是 `configs/tests/default.jsonc` 中的 `backup_3` 和 `backup_4` 使用 Responses API，继续沿用 Chat 专用终止规则会让测试替身与真实 provider 的字段和事件语义不一致。

需要从“通用 SSE 录制器”与“协议 codec”两个层次重新组织：transport 负责 HTTP、匹配、并发和生命周期；协议 codec 负责某一种 provider stream 的事件名、payload 和终止语义。这样既能保留 LiteLLM 原本的请求构造与解析链路，也能让同一套 cassette 基础设施扩展到 Anthropic Messages，而不在没有真实资源时制造伪造基线。

## What Changes

- 将模型 stream cassette 的 frame 改为协议中立结构，保留 `event`、`encoding` 和完整 payload；不再由通用 loader 假定所有协议都使用 `[DONE]`。
- 新增协议 codec 注册表，并实现 `openai_chat_sse` 与 `openai_responses_sse`；Responses 事件按原始 SSE 事件名和 JSON object 保存，`response.completed` 作为协议终止事件。
- 实现 `anthropic_messages_sse` 的 codec 和完整手写 reasoning/tool cassette；本变更不包含真实 Anthropic 网络请求或 Anthropic adapter 的完整业务集成测试。
- 让 record/replay transport 根据 cassette metadata 的协议选择 codec；record 继续把原始字节转发给 LiteLLM，replay 为每个请求创建独立的 wire stream。
- 保留显式 `request_reusable` 和 `session_sequence` 两种回放策略，协议扩展不改变其并发隔离语义。
- 增加 Responses 的手写基础文本资源、场景、配置和 LiteLLM 真实异步调用测试；现有 Chat 资源和测试继续作为回归基线。
- 补齐 Chat Completions 与 Responses 各自的稳定语义基线：两者默认都覆盖 reasoning、tool call、工具结果后的再次 reasoning 和最终文本；Responses 另保留显式 reasoning + text 场景作为轻量协议测试基线。
- 将 Chat Completions 默认测试基线固定为 `reasoning-tool`，将 Responses 默认测试基线固定为 `responses-reasoning-tool`；基础文本、仅 reasoning + text、特定工具和其它协议变体只能通过 `configs/tests/` 中的显式配置切换。
- 为 Chat 多 interaction 回放增加不暴露消息正文的安全结构匹配字段，使首轮请求和携带工具结果的后续请求可被 cassette 唯一选择。
- 增加 Responses 双 `read_file` 并发工具调用场景，手写 cassette 刻意交错两个 function call 的参数 delta，并要求解析和业务关联按 `item_id`/`call_id` 保持独立。
- 继续使用 `configs/tests/` 中的 JSONC 控制测试 transport。协议由所选 cassette 的 metadata 决定，不在 scenario 中重复配置，避免 provider 配置、测试运行配置和上游协议出现三处真相。
- 对未知协议、非法事件、缺少协议终止事件、请求不匹配和不完整录制明确失败；不联网兜底、不返回默认数据。

## Capabilities

### New Capabilities

- `model-stream-chunk-mock-service`: 为单元、集成和 E2E 测试提供协议可扩展的上游模型 stream asset、record/replay transport、场景选择和并发安全回放能力。

### Modified Capabilities

无。本变更只扩展测试基础设施，不修改生产业务事件协议或 provider API mode 的既有行为。

## Impact

- 测试基础设施：扩展 `app/testing/model_stream/` 的 frame、asset loader、SSE parser、codec registry 和 transport。
- 测试资产：保留现有 `handwritten/openai_chat`，新增 Chat 完整 reasoning/tool loop cassette、Responses 完整 reasoning/tool loop cassette，以及 Anthropic Messages reasoning/tool cassette。
- 测试配置：新增 Responses 场景配置，原有 Chat 配置保持可用；不把 API key 或 provider endpoint 写入测试控制配置。
- 测试覆盖：Chat Completions 和 Responses 均覆盖真实 LiteLLM 异步 HTTP 路径，以及 reasoning、tool call、工具结果和最终文本的 E2E 结果；Anthropic 覆盖 codec、asset、wire round-trip，完整 adapter 业务集成另行建设。
- 生产边界：只在显式设置 `BOXTEAM_TEST_MODEL_STREAM_CONFIG` 时安装测试 transport；生产请求、业务事件、前端和工作区数据目录不变。
- 录制产物：完整 cassette、incomplete 诊断和临时输出继续写入测试输出目录，不自动提升为长期 fixture。
