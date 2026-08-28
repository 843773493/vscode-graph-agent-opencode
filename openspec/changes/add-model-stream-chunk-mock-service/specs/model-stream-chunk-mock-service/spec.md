## Purpose

为单元、集成和 E2E 测试提供不访问真实模型的上游模型 stream asset、协议编解码、record/replay transport、场景选择和并发安全回放能力。该能力只模拟 provider HTTP/SSE 输入，业务事件仍由真实业务链路产生。

## ADDED Requirements

### Requirement: 场景必须选择一个协议可识别的 cassette

测试运行在 `record` 或 `replay` 模式时，系统 MUST 通过 `configs/tests/` 下的 JSONC 配置选择 scenario；scenario MUST 引用 fixture root 内的 cassette。cassette metadata 中的 `protocol` MUST 是协议唯一来源，scenario MUST NOT 重复声明协议、transport mode 或 replay policy。

#### Scenario: Chat 场景被显式选择

- **GIVEN** 测试配置选择 `basic-text` scenario
- **AND** `basic-text` cassette metadata 的 `protocol` 为 `openai_chat_sse`
- **WHEN** 测试进程启动 model stream transport
- **THEN** transport 加载该 Chat cassette 并继续使用真实 LiteLLM 请求路径

#### Scenario: cassette 路径越界被拒绝

- **GIVEN** scenario 的 asset 路径解析后位于 fixture root 之外
- **WHEN** loader 加载 scenario
- **THEN** loader 必须抛出明确的 asset 错误，且不得读取越界文件

### Requirement: 协议 registry 必须区分 Chat、Responses 和 Anthropic

registry MUST 识别 `openai_chat_sse`、`openai_responses_sse` 和 `anthropic_messages_sse`。前两个协议 MUST 提供可运行的 decode、encode、terminal 判断和 stream 校验；Anthropic MUST 可被发现但以未实现错误拒绝运行。未知协议 MUST NOT 默认为 Chat。

#### Scenario: Responses codec 被选择

- **GIVEN** cassette metadata 的 `protocol` 为 `openai_responses_sse`
- **WHEN** transport 创建 replay response
- **THEN** transport 使用 Responses codec 编码 frame，并保留每个 frame 的 SSE event 名称

#### Scenario: Anthropic 接口暂不运行

- **GIVEN** 请求或 cassette 声明 `anthropic_messages_sse`
- **WHEN** transport 尝试安装或 codec 尝试处理 frame
- **THEN** 系统必须抛出包含 protocol id 和“未实现”信息的明确错误，且不得伪造回放数据或触网

#### Scenario: 未知协议快速失败

- **GIVEN** cassette metadata 的 protocol 不是 registry 中的稳定 id
- **WHEN** loader 或 transport 处理 cassette
- **THEN** 系统必须报告未知协议，并拒绝继续运行

### Requirement: cassette 必须保存协议事件和完整 payload

每个 provider frame MUST 保存 `kind`、`encoding`、`payload` 和可选 `event`。loader MUST 保留未知 JSON 字段；通用校验只要求结构合法、恰好一个 terminal frame 且 terminal 位于末尾，协议专用约束由 codec 校验。

#### Scenario: Responses event 名称和未知字段被保留

- **GIVEN** Responses frame 包含 `event=response.output_text.delta` 和 provider 自定义 JSON 字段
- **WHEN** cassette 被加载再编码回 SSE
- **THEN** event 名称、JSON payload 和未知字段必须仍然存在

#### Scenario: Chat 使用 `[DONE]` 终止

- **GIVEN** Chat cassette 最后一个 frame 是文本 payload `[DONE]`
- **WHEN** Chat codec 校验 stream
- **THEN** 该 frame 被识别为唯一 terminal，并可编码为 `data: [DONE]` SSE frame

#### Scenario: Responses 使用 completed event 终止

- **GIVEN** Responses cassette 最后一个 frame 的 event/type 为 `response.completed`
- **WHEN** Responses codec 校验 stream
- **THEN** 该 frame 被识别为唯一 terminal，且其 JSON response 字段不被裁剪

### Requirement: Chat 和 Responses 必须各有完整语义回放基线

Chat Completions 与 OpenAI Responses MUST 各自提供长期手写 cassette 和可运行 E2E 场景。两者默认场景 MUST 覆盖首轮 reasoning、tool call、工具结果驱动的后续 reasoning 和最终可见文本；Responses 另有显式 reasoning + text 场景供轻量协议测试使用。涉及 tool loop 的 E2E MUST 同时断言 provider 上游记录、业务 tool call 事件、工具执行结果和最终 assistant 文本。Chat cassette MUST 使用 `message_roles` 等不含消息正文的安全结构字段区分 interaction；Responses cassette MUST 使用 `input_types` 区分 interaction。

默认测试配置 MUST 使用体现完整链路的稳定基线：Chat Completions 默认选择 `reasoning-tool`，Responses 默认选择 `responses-reasoning-tool`，两者都使用最简单的 `read_file(README.md)` 测试工具。基础文本、Responses reasoning + text 或其它特定工具需求 MUST 通过 `configs/tests/` 下的显式 JSONC 配置切换，不得改变默认基线的语义。

#### Scenario: Chat 完整 tool loop 被真实业务链路消费

- **GIVEN** Chat cassette 的首轮 response 含 `reasoning_content` 和分片 `tool_calls`
- **AND** 后续 interaction 匹配含 `tool` 消息的 `message_roles`
- **WHEN** E2E 通过真实 Chat model、LiteLLM async HTTP client 和 workspace tool 执行请求
- **THEN** provider log 必须保存 reasoning 和 tool call，业务 trace 必须有 tool start/end，工具结果后的 assistant message 必须保存最终可见文本

#### Scenario: Responses 完整 tool loop 被真实业务链路消费

- **GIVEN** Responses cassette 的首轮 response 含 reasoning output item 和 function call
- **AND** 后续 interaction 匹配 `reasoning`、`function_call`、`function_call_output` 等 `input_types`
- **WHEN** E2E 通过真实 Responses model、LiteLLM `aresponses` 和 workspace tool 执行请求
- **THEN** provider log 必须保存两轮 reasoning、function call、工具输入输出和最终 message，业务 trace 必须有 tool start/end，assistant message 必须保存最终可见文本

### Requirement: replay 必须继续真实 LiteLLM 处理链路

replay MUST 把 cassette frame 编码成 provider wire bytes，返回给 LiteLLM 使用的 async HTTP client；MUST NOT 直接返回业务事件、SDK chunk 或固定文本。每个请求 MUST 创建独立的 response stream 和迭代状态。

#### Scenario: Chat replay 进入 Chat model parser

- **GIVEN** Chat scenario 命中一个 interaction
- **WHEN** `BoxteamLiteLLMChatModel` 通过 LiteLLM 发起流式请求
- **THEN** LiteLLM 必须收到 Chat SSE 并产生正常 SDK/LangChain chunk，业务层可以继续生成自己的事件

#### Scenario: Responses replay 进入 Responses parser

- **GIVEN** Responses scenario 命中一个 interaction
- **WHEN** `BoxteamOpenAIResponsesModel` 通过 LiteLLM `aresponses` 发起流式请求
- **THEN** LiteLLM 必须收到带 event 名称的 Responses SSE，并产生正常 Responses/LangChain 转换结果

### Requirement: request_reusable 必须支持并发独立回放

默认 `request_reusable` 策略 MUST 把 cassette 作为只读共享值，每次命中都创建独立 response stream。多个并发请求 MUST NOT 共享 frame iterator、当前索引、关闭状态或取消状态。

#### Scenario: 多会话并发从同一个 frame 0 开始

- **GIVEN** 24 个并发请求匹配同一个 Chat 或 Responses interaction
- **WHEN** 它们同时读取 response stream
- **THEN** 每个请求都必须收到完整且相同的 frame 序列，且 hit count 等于请求数

#### Scenario: 一个请求提前关闭不影响其他请求

- **GIVEN** 两个请求共享同一个 interaction 的 cassette
- **WHEN** 第一个 response 被取消或提前关闭
- **THEN** 第二个 response 必须仍能从自己的 frame 序列独立读取到 terminal

### Requirement: session_sequence 必须按显式会话隔离顺序

`session_sequence` MUST 要求调用方提供 `replay_session_id`，按 sequence id 和 step 唯一选择 interaction，并为每个会话维护独立游标。缺少会话、步骤断裂或候选不唯一 MUST 失败。

#### Scenario: 两个会话各自消费同一序列

- **GIVEN** cassette 有 `tool-loop` 的 step 0 和 step 1
- **WHEN** session A 消费两步，session B 只消费第一步
- **THEN** A 的第二次请求必须得到 step 1，B 的第一次请求必须仍得到 step 0

#### Scenario: 没有 replay session context

- **GIVEN** transport 使用 `session_sequence`
- **WHEN** 请求没有显式 session id
- **THEN** transport 必须抛出要求 `replay_session_id` 的匹配错误

### Requirement: record 必须旁路转发并按协议完整提交

record MUST 把上游原始 response bytes 原样转发给 LiteLLM，同时通过对应 codec 解析 provider frame。只有正常读取结束且观察到协议 terminal 时才原子写入完整 cassette；中断、非法 frame 或缺少 terminal MUST 只能生成 incomplete 诊断。

#### Scenario: Responses record 保留原始 event 行

- **GIVEN** 上游返回带 `event: response.output_text.delta` 的 Responses SSE
- **WHEN** LiteLLM 读取该 response 且 stream 正常完成
- **THEN** LiteLLM 收到的 bytes 不得被重写，录制 cassette 必须保存 event 名称和 payload

#### Scenario: 缺少协议 terminal 不生成可回放 cassette

- **GIVEN** 上游 stream 在任意 Responses/Chat terminal 之前结束
- **WHEN** recorder 完成读取
- **THEN** 系统必须写入 incomplete 诊断，并不得写入看似完整的 cassette

### Requirement: 测试资产、业务 expectation 与配置必须分层

手写 asset、录制 asset、scenario 和业务 expectation MUST 分别存储。测试可以复用同一个 scenario，但 MUST NOT 把业务事件 payload 写入 provider cassette，也 MUST NOT 在 scenario 复制 transport 参数。

#### Scenario: 一个 Responses asset 被多个测试层复用

- **GIVEN** Responses scenario 同时被 provider 集成测试和业务集成测试引用
- **WHEN** 两个测试加载 scenario
- **THEN** 两者必须读取同一协议 cassette，而业务断言由各自 expectation 完成

#### Scenario: 没有真实 Anthropic 资源时不制造 fixture

- **GIVEN** registry 已有 Anthropic protocol id 但 fixture root 没有 Anthropic cassette
- **WHEN** 测试发现可用协议和资源
- **THEN** 测试只能验证接口和明确未实现行为，不得生成猜测性的 Anthropic response fixture

### Requirement: 错误和敏感信息边界必须可诊断且安全

配置、asset、协议、匹配、回放顺序和 transport 生命周期错误 MUST 显式失败。匹配诊断可以包含 request id、body keys、有限安全字段和脱敏 URL，但 MUST NOT 包含 Authorization、Cookie、API key 或完整敏感 request body。

#### Scenario: 不匹配诊断不泄漏凭据

- **GIVEN** 请求 URL query 和 Authorization header 含敏感值且 model 不匹配
- **WHEN** matcher 抛出错误
- **THEN** 错误必须保留 scenario、request id 和候选信息，但不得出现敏感值

#### Scenario: transport 安装过晚

- **GIVEN** LiteLLM async client 已经创建
- **WHEN** 测试尝试安装 model stream transport
- **THEN** 安装必须失败并说明必须在第一次模型请求前完成
