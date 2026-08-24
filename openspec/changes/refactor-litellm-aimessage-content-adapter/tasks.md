## 1. 内容适配边界

- [x] 1.1 盘点当前 LiteLLM 普通响应、Responses 响应和流式 delta 的字段形状，确定有序 content blocks 的字段规则。
- [x] 1.2 实现 LiteLLM → `AIMessage.content` 的有序 carrier/provider block 适配器，保留原始 content/reasoning 字段，不再把 reasoning 字段写入最终 `additional_kwargs` 或 `content[].extras`。
- [x] 1.3 保持 `AIMessage.tool_calls`、`ToolMessage.tool_call_id` 和 LangGraph 工具路由的标准契约。

## 2. 历史请求投影

- [x] 2.1 实现共享的有序 content block 提取、可见文本/summary 提取和 source/target provider 能力投影逻辑。
- [x] 2.2 更新 Chat Completions、Responses 和模型切换路径，使其只从 `AIMessage.content` 读取 reasoning 数据，并实现 encrypted replay 的来源匹配和 server-owned 字段过滤。
- [x] 2.3 确保投影返回临时消息或请求 payload，不修改 checkpoint、rollout JSONL、SQLite 或源 `AIMessage`。

## 3. 流式、checkpoint 和历史读取

- [x] 3.1 更新流式 chunk 合并与最终 assistant 提交，使每个稳定 assistant 只写入一条 canonical AIMessage。
- [x] 3.2 更新 checkpoint full restore、SQLite reasoning/tool projection、Web 安全摘要和 final response 读取，使其不依赖 `additional_kwargs` reasoning 备份。
- [x] 3.3 重新生成混合 provider 的常驻 128 Turn 测试资源，并移除旧 payload 文件和旧重复布局依赖。

## 4. 测试与验证

- [x] 4.1 更新 provider unit tests，覆盖纯文本、普通 reasoning、summary/encrypted、thinking、null、tool call 和跨 provider 投影。
- [x] 4.2 增加 AIMessage/ToolMessage、LangGraph checkpoint roundtrip、SQLite projection 和不可变源消息测试。
- [x] 4.3 增加 streaming finalization 测试，确认不会产生 chunk 级 canonical 消息。
- [x] 4.4 使用复制到 `out/tests/.../workspace/` 的测试工作区运行集成测试，确认 128 Turn 资源可加载并按 provider 能力得到正确投影。
- [x] 4.5 执行 Python 静态分析、相关 pytest、OpenSpec 严格校验和 `git diff --check`，修复实现细节问题。

## 5. 有序 content Schema

- [x] 5.1 新增 provider content Pydantic Schema，定义 `reasoning_content`、`reasoning_items` carrier 和 provider 原始 block 的校验、顺序及未知字段保留规则。
- [x] 5.2 更新示例、LiteLLM/Responses/Anthropic 适配器和 canonicalizer，使所有稳定 `AIMessage.content` 都经过 Schema 校验并保留 carrier 顺序。
- [x] 5.3 更新 reasoning、summary、encrypted、可见文本、checkpoint roundtrip 和 provider projection 测试，覆盖 carrier block 的顺序与不可变性。
- [x] 5.4 运行 provider 单元测试、Schema 示例验证、Python 静态检查和 OpenSpec 严格校验。
