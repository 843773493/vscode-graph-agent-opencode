## 1. 顶层模型与测试配置

- [x] 1.1 在 `app/testing/model_stream/` 固定 HTTP byte chunk、SSE frame、Provider frame、SDK chunk 和 Business event 的边界。
- [x] 1.2 在 `configs/tests/` 增加 JSONC transport 配置和 schema，支持 `off`、`record`、`replay`。
- [x] 1.3 保持 scenario 只引用 asset 和业务 expectation，不重复 transport 或协议配置。
- [x] 1.4 校验 fixture root、scenario id、asset 相对路径和 artifact root 的安全边界。
- [x] 1.5 保持 `_local` 配置约定，不把 API key、Authorization 或 provider 私有 endpoint 写入共享测试控制配置。

## 2. 协议中立 frame 与 codec registry

- [x] 2.1 扩展 `StreamFrame`，保存可选 SSE `event`、encoding 和完整 payload。
- [x] 2.2 抽取 `StreamProtocolCodec` contract 和按 protocol id 查找的 registry。
- [x] 2.3 实现 `openai_chat_sse` codec，兼容 JSON/text data frame 和 `[DONE]`。
- [x] 2.4 实现 `openai_responses_sse` codec，保留 Responses event 名称、JSON 字段并识别 `response.completed`。
- [x] 2.5 注册 `anthropic_messages_sse` codec 接口，runtime 明确报告未实现，不创建伪造资源。
- [x] 2.6 将 cassette loader 的终止校验从全局 `[DONE]` 改为通用结构校验加 codec 专用校验。
- [x] 2.7 增加 Chat/Responses/Anthropic/未知 protocol 的 codec 单元测试和目录规范检查。

## 3. provider stream 资产与场景

- [x] 3.1 保留并校验现有 Chat handwritten assets、tool-call 分片、reasoning 和 usage 场景。
- [x] 3.2 增加 Responses handwritten 基础文本 cassette，覆盖 created、output item/content/text delta 和 completed event。
- [x] 3.3 增加 Responses scenario、业务 expectation 和独立 JSONC 测试配置。
- [x] 3.4 在资产测试中验证 Responses event/payload 原样加载和重新编码。
- [x] 3.5 明确 Anthropic 暂无真实资源，测试只验证接口发现和未实现错误。
- [x] 3.6 保持 recorded cassette 只能通过显式 promotion 进入长期资产目录。

## 4. matcher 与 replay policy

- [x] 4.1 保持严格 method、脱敏 URL 和安全 body 子集匹配。
- [x] 4.2 保持敏感 header/query 不进入错误诊断。
- [x] 4.3 保持 `request_reusable` 每个请求创建独立 response stream 和 frame iterator。
- [x] 4.4 保持 `session_sequence` 的显式 session context、连续 step 和唯一候选校验。
- [x] 4.5 增加 Chat 与 Responses 共用 replay coordinator 的并发回归测试。

## 5. LiteLLM transport 与录制

- [x] 5.1 让 replay transport 根据 cassette protocol 获取 codec，并拒绝 Anthropic 未实现 codec。
- [x] 5.2 让 replay wire encoder 保留 event 行、JSON payload 和协议终止格式。
- [x] 5.3 让 SSE recorder 保存 event 名称，使用 codec 判断 terminal，不再写死 `[DONE]`。
- [x] 5.4 保持 record 原始 bytes 旁路转发给 LiteLLM，不改变请求和响应 wire 内容。
- [x] 5.5 对 Chat 做完整回归：正常 terminal、缺 terminal、半 frame、异常关闭和 incomplete artifact。
- [x] 5.6 对 Responses 做完整回归：event 顺序、completed terminal、未知字段和 incomplete artifact。
- [x] 5.7 保持 LiteLLM client 生命周期：第一次请求前安装，测试结束恢复并关闭。

## 6. provider、集成与 E2E 验证

- [x] 6.1 保持 Chat transport 集成测试覆盖 LiteLLM async client 注入和并发独立读取。
- [x] 6.2 使用 `BoxteamOpenAIResponsesModel` 和真实 `litellm.aresponses` 验证 Responses replay，不访问真实 provider。
- [x] 6.3 验证 Responses 请求 URL、model、stream match 与 `backup_3` 形态兼容。
- [x] 6.4 验证 Chat 与 Responses 在同一测试进程中的多会话并发不会共享迭代器或 session cursor。
- [x] 6.5 保持 Chat E2E 使用 replay transport 验证业务事件和最终 assistant 输出。
- [x] 6.6 增加 Responses 到业务输出的集成断言；Anthropic 本次不执行真实响应测试。
- [x] 6.7 保持测试工作区、日志和其他产物遵循 `out/tests/` 规则。

## 7. 安全、诊断与资产晋升

- [x] 7.1 保持 request mismatch 错误包含 scenario、asset、request id 和候选 interaction，但不泄漏凭据。
- [x] 7.2 保持 artifact root 和 fixture root 的路径边界校验。
- [x] 7.3 保持 incomplete artifact 与完整 cassette 分离，禁止自动晋升。
- [x] 7.4 保持 record 只保存安全 request match 字段和安全 response headers。
- [x] 7.5 增加 unknown protocol、Anthropic 未实现和 Responses 非法 terminal 的明确错误断言。

## 8. 校验与交付

- [x] 8.1 运行 model stream 单元、集成和 Responses provider 测试。
- [x] 8.2 运行 Chat E2E 回归并确认不触网。
- [x] 8.3 运行相关 Python 静态分析，修复新增代码的 lint/type 问题。
- [x] 8.4 运行 `openspec validate add-model-stream-chunk-mock-service --strict`。
- [x] 8.5 复查文档中的 protocol id、API mode、asset/source、frame/terminal 和配置命名无语义冲突。

## 9. 完整 reasoning/tool loop 基线

- [x] 9.1 扩展安全 request matcher/recorder，保存 Chat `message_roles`，不保存消息正文，并保持现有 Chat/Responses cassette 兼容。
- [x] 9.2 增加 Chat 完整 reasoning、分片 tool call、工具结果后的 reasoning/文本 response cassette、scenario、expectation 和 JSONC 配置。
- [x] 9.3 增加 Chat 完整 tool loop E2E，断言 provider log、reasoning、tool_call start/end、工具结果和最终 assistant 文本。
- [x] 9.4 校验并补强 Responses 完整 reasoning/tool loop cassette、scenario、expectation 和 E2E，断言两轮 provider response 与业务结果一致。
- [x] 9.5 增加 Chat/Responses 完整场景的资产、matcher、provider 和集成回归测试，覆盖并发请求不会共享 frame iterator。
- [x] 9.6 运行相关静态分析、单元/集成/E2E 和 `openspec validate add-model-stream-chunk-mock-service --strict`，审查生成的 E2E 数据产物。

## 10. 默认完整流程与显式场景切换

- [x] 10.1 将 Chat 默认 JSONC 配置切换到 `reasoning-tool`，使用 `read_file(README.md)` 作为最简单测试工具。
- [x] 10.2 增加 Responses 默认 `responses-reasoning-tool` 手写 cassette、scenario、expectation 和 E2E 断言，并保留 `responses-reasoning-text` 轻量场景。
- [x] 10.3 增加 Chat/Responses 基础文本和 reasoning + text 场景配置，并保留显式工具循环配置作为特殊需求切换入口。
- [x] 10.4 将默认 Chat/Responses 集成与系统测试接入对应完整基线，并完成静态分析、单元、集成、E2E 和严格 OpenSpec 校验。
