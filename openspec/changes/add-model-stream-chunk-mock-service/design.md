## Context

模型调用的业务链路已经支持三种 provider API mode：Chat Completions、OpenAI Responses 和 Anthropic Messages。测试替身不应把这些协议的上游事件直接转换为业务 block 或 `message.v1` 事件；它应该只提供 HTTP 层接收到的 provider stream，让 LiteLLM、SDK/LangChain adapter 和业务聚合逻辑继续真实运行。

现有实现已经具备 cassette、严格 request matcher、LiteLLM async client 注入、录制和两种回放策略，但 frame 模型将 `[DONE]` 视为全局规则。这会丢失 Responses 的 `event:` 行，也会让以后添加 Anthropic 时把协议差异继续堆进 transport。新的设计把协议差异收敛到 codec，保持其余层稳定。

## Terminology

- **HTTP byte chunk**：HTTP client 一次读取的字节片段，可能只包含半个 SSE frame；不作为长期资产的边界。
- **SSE frame**：由 SSE 空行分隔、包含可选 `event:` 和一个或多个 `data:` 行的 provider wire 事件，是回放的顺序单位。
- **Provider frame**：codec 将一个 SSE frame 解码后的协议事实，包含协议事件名和完整 payload；这是 cassette 保存的核心数据。
- **SDK chunk**：LiteLLM/OpenAI SDK/LangChain 解析 provider frame 后的对象，不写入 cassette。
- **Business event**：业务层的 block、message、trace 或 `message.v1` 事件，由真实业务链路产生，使用独立 expectation 验证。
- **Cassette**：带 metadata、request matcher、response headers 和 provider frames 的 JSON 资产。
- **Scenario**：引用一个 cassette 并声明业务 expectation 标识的 JSONC manifest；不复制 transport 参数或响应数据。
- **Protocol codec**：知道某种 provider SSE 事件格式、事件编码和终止条件的组件。
- **model stream transport**：进程内 `httpx.AsyncBaseTransport`，不是监听端口的 mock HTTP server。正文统一使用此名称。

命名约定：Python 标识符和 JSON 字段使用 `snake_case`；scenario id、asset id 和文件名使用 `kebab-case`；协议 id 使用稳定的 `snake_case` 枚举值：`openai_chat_sse`、`openai_responses_sse`、`anthropic_messages_sse`。自然语言中的“chunk”必须说明它是 HTTP byte chunk、SSE frame、Provider frame、SDK chunk 还是 Business event。

## Goals / Non-Goals

**Goals:**

- 以一个协议中立的 cassette 模型保存手写 provider frame 和 E2E 录制的真实 provider frame。
- 真实执行请求构造、LiteLLM HTTP 调用、协议解析、SDK/LangChain 转换和业务事件聚合。
- 现在实现 Chat Completions SSE 和 OpenAI Responses SSE；为 Anthropic Messages 保留可发现、可诊断的 codec 接口。
- 让 protocol codec 决定 `event`、payload 编码和终止事件，通用 transport 不包含 provider 专用判断。
- 多会话并发共享只读 cassette；每个请求独立拥有 response stream，`request_reusable` 不共享迭代器，`session_sequence` 只在显式 session context 内维护游标。
- 用 `configs/tests/*.jsonc` 选择运行模式和 scenario，避免大量环境变量和重复配置。
- 将 provider 上游资产与业务 expectation 分离，使业务协议变更不要求修改 provider frame。
- 在资产、协议、匹配、录制和生命周期出错时快速失败并保留可诊断信息。

**Non-Goals:**

- 不定义或冻结 `message.v1`、block projection、Trace 或前端事件协议。
- 不把 SDK chunk、Provider frame 和 Business event 混存为同一种数据。
- 不通过修改 provider endpoint 环境变量把请求改发到 localhost，不新增必须监听端口的服务。
- 不在本次实现 Anthropic 真实 codec、手写资源、录制资源或真实上游测试；只有接口注册和显式未实现错误。
- 不复现 TCP packet 边界、真实 provider 时延分布或网络背压模型。
- replay 匹配失败时不连接真实 provider、不返回默认文本、不静默跳过。

## Top-Level Architecture

```text
configs/tests/*.jsonc
        │  mode / scenario / replay policy
        ▼
scenario manifest ──► cassette metadata.protocol
        │                        │
        │                        ▼
        └──────────────► ProtocolCodecRegistry
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
             RequestMatcher              Codec
                    │                 decode/encode/terminal
                    ▼                         │
          ReplayCoordinator ◄──── ModelStreamHTTPTransport
                    │                         │
                    └──────────► LiteLLM async HTTP client
                                                │
                         Chat model / Responses model / future Anthropic model
                                                │
                                      SDK chunk / block conversion
                                                │
                                      Business event expectations
```

配置只选择 scenario 和 transport 行为；scenario 只引用资产；资产 metadata 是协议的唯一来源。provider endpoint、API key、模型参数仍来自真实 provider 配置，transport 不改写请求 URL。record/replay 安装在 LiteLLM 第一次创建 async client 之前，复用生产调用路径。

## Protocol Model

### Stable protocol ids

| protocol id | API mode | 本次状态 | 终止语义 |
| --- | --- | --- | --- |
| `openai_chat_sse` | `chat_completions` | 已实现 | 最后的 `data: [DONE]` |
| `openai_responses_sse` | `responses` | 已实现 | `event: response.completed` 的 JSON event |
| `anthropic_messages_sse` | `anthropic_messages` | 只注册接口 | 由后续 Anthropic codec 定义，本次不可运行 |

### Canonical provider frame

Cassette 中的 frame 使用以下结构：

```json
{
  "kind": "data",
  "event": "response.output_text.delta",
  "encoding": "json",
  "payload": {"type": "response.output_text.delta", "delta": "文本"}
}
```

`event` 可以为空，以兼容没有显式 SSE `event:` 行的 Chat Completions。`payload` 保留完整 JSON object 和未知字段，不做 SDK 层裁剪。`kind=data` 表示普通 provider event；`kind=done` 表示由 codec 认定的协议终止 event。Chat 的 done 是 `encoding=text`、payload `[DONE]`；Responses 的 done 是 `encoding=json`、event `response.completed` 以及完整的 completed response object。

通用校验只负责结构合法、done 位于末尾且只出现一次；协议 codec 负责事件名、payload 类型和终止条件。这样不会把 Chat 的 `[DONE]` 误套到 Responses 或 Anthropic。

### Codec registry

协议 codec 提供四个能力：

1. 将 SSE 的 `event` 和合并后的 `data` 字符串解码为 `StreamFrame`；
2. 将 `StreamFrame` 编码回 LiteLLM 可读取的 SSE bytes；
3. 判断某 frame 是否是终止 frame；
4. 校验单个 frame 和完整 stream 的协议约束。

registry 按 `metadata.protocol` 查找 codec。未知协议直接报告资产错误；已注册但 `runtime_supported=false` 的 Anthropic codec 在 transport 安装或真实 decode/encode 时报告明确的未实现错误。不存在“把未知协议当 Chat”或“自动使用默认 codec”的降级路径。

## Asset and Config Layout

```text
configs/tests/
├── model_stream.jsonc                         # Chat 默认 reasoning/tool loop
├── model_stream_responses.jsonc               # Responses 默认 reasoning/tool loop
├── model_stream_chat_basic.jsonc              # Chat 基础文本切换
├── model_stream_chat_tool.jsonc               # Chat 显式工具场景切换
├── model_stream_responses_basic.jsonc         # Responses 基础文本切换
├── model_stream_responses_tool.jsonc          # Responses 显式工具场景切换
├── model_stream_responses_reasoning_text.jsonc # Responses 显式 reasoning/text 切换
└── model_stream_schema.jsonc                  # 共享配置 schema

tests/fixtures/model_stream/
├── handwritten/
│   ├── openai_chat/*.json
│   └── openai_responses/*.json
├── recorded/
│   ├── openai_chat/*.json
│   └── openai_responses/*.json
├── scenarios/*.jsonc
└── expectations/*.{json,jsonc}
```

`handwritten` 是可读、稳定、最小的 provider 事实；`recorded` 是从真实上游采集并经过审查后显式提升的长期资产；两者经过同一 loader。Anthropic 目录不提前创建伪造资源，等有真实上游响应后再增加 `handwritten/anthropic_messages` 或 `recorded/anthropic_messages`。

业务 expectation 只检查模型输出、reasoning/tool block、结束状态或业务事件，不复制完整 provider frame。当前 Chat 默认由 `reasoning-tool` 提供完整工具循环，Responses 默认由 `responses-reasoning-tool` 提供完整工具循环；`responses-reasoning-text` 和其它基础/特定工具场景通过对应 JSONC 配置显式选择。一个 scenario 可被单元、集成和 E2E 测试复用，测试层按自身粒度决定检查 provider frame、SDK chunk 还是业务事件。

## Request Matching and Replay

- matcher 比较 HTTP method、经过敏感 query 脱敏的 URL 和 cassette 中显式声明的 request body 子集。
- asset 不保存完整 Authorization、Cookie、API key 或完整 request body；录制只保存用于匹配的 `model`、`stream`、Responses `input_types` 和 Chat `message_roles` 等安全字段及脱敏 URL。因此复杂 tool loop 可以在不保存消息正文的前提下区分首轮请求和工具结果后的请求。
- `request_reusable` 是默认策略。cassette 和 frame 是只读值对象，每个命中的 interaction 创建自己的 `_ReplayByteStream` 和异步迭代状态；24 个并发请求可以独立从 frame 0 开始。
- `session_sequence` 需要调用方显式建立 `replay_session_id`，按 `(sequence_id, step)` 唯一选择 interaction，并为每个 session 维护自己的游标。没有 context、顺序不唯一或步骤缺失都直接失败。
- replay 不自动触网；transport 未命中时错误中包含 scenario、asset、request id、已脱敏的请求摘要和候选 interaction 信息。

## Record Flow

```text
upstream response bytes
        ├── 原始 bytes ──► LiteLLM
        └── SSE parser ──► codec.decode ──► frames
                                             │
                       codec terminal + 正常 close
                                             ▼
                                  原子写入 recorded cassette
```

record 只在完整读取且 codec 观察到协议终止事件后提交 cassette。连接中断、取消、非法 event、缺少 terminal 或未完成 SSE frame 生成 `.incomplete.json` 诊断，不生成可回放 cassette。record 不改变原始 bytes，因此 LiteLLM 仍接收真实上游数据和字段。

## Protocol-specific Scope

### Chat Completions

保留现有 Chat 资产，支持 JSON data frame、文本 data frame 和 `[DONE]`。Chat 的 `event` 通常为空；若上游提供 event，codec 保留它但不依赖它判断终止。默认 `model_stream.jsonc` 选择 `reasoning-tool`，使用 `read_file(README.md)` 体现首轮 reasoning、分片 `tool_calls`、工具结果和后续 reasoning/文本的完整流程。Chat 多 interaction 使用安全的 `message_roles` 匹配，不能把用户消息、工具参数或工具结果正文写入 cassette；基础文本或其它工具需求使用显式配置。

### OpenAI Responses

保存 `response.created`、`response.output_item.*`、`response.content_part.*`、`response.output_text.*`、reasoning/tool 相关 event 以及 `response.completed` 中的完整 JSON 字段。默认 `model_stream_responses.jsonc` 选择 `responses-reasoning-tool`，覆盖首轮 reasoning、function call、工具结果后的再次 reasoning、最终 message 和 `response.completed`；`responses-reasoning-text` 由 `model_stream_responses_reasoning_text.jsonc` 显式选择，Responses 工具循环另有 `model_stream_responses_tool.jsonc` 显式入口，并通过 LiteLLM `aresponses` 真实解析验证。

### Anthropic Messages

registry 预留 `anthropic_messages_sse`，其 codec contract、协议 id 和错误边界已经固定，但本次不声称可以运行。没有真实上游响应时不添加手写“猜测数据”，也不把 Anthropic frame 当 Chat 或 Responses 回放。

## Error and Security Boundaries

- 配置、scenario、asset、协议、matcher、回放顺序和 transport 生命周期错误必须显式抛出。
- 诊断只允许展示脱敏 URL、body keys、有限的安全匹配字段和 request id；禁止回显 Authorization、Cookie、API key 或完整敏感 body。
- cassette 只能从配置的 fixture root 解析，asset 相对路径不能跳出 root；录制 artifact 只能写入配置的 artifact root。
- 生产未显式启用时不安装测试 transport；应用启动时发现 LiteLLM client 已经创建则直接失败，避免“部分请求走真网、部分请求走回放”。

## Verification Strategy

1. asset/codec 单元测试验证三种 protocol id、frame event 保留、Chat `[DONE]`、Responses terminal 和 Anthropic 未实现错误。
2. transport 集成测试验证原始 bytes 转发、完整/不完整 record、Chat/Responses replay、脱敏诊断和多请求独立迭代器。
3. provider 集成测试使用真实 `BoxteamLiteLLMChatModel` 与 `BoxteamOpenAIResponsesModel`，只通过注入的 LiteLLM async client，不访问真实 provider。
4. Chat 与 Responses 默认都使用手写完整 tool loop cassette；Responses 额外保留显式 reasoning + text cassette。通过 in-process provider model/transport 验证 reasoning、tool call、工具结果和最终文本从上游事件到业务输出的链路；Anthropic 本次不跑真实响应测试。
5. OpenSpec strict validation、ruff 和相关 pytest 必须通过；正式测试产物遵循 `out/tests/<test-path>/` 目录规则。
