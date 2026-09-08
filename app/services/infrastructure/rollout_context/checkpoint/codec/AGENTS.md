# 目录用途

定义 checkpoint 消息适配的语义 metadata 和有序 canonical group 编解码。

# 可修改内容

- MessageCodec 边界的语义字段白名单、content carrier 与 canonical item group 转换。

# 不可修改内容

- 不得读写 JSONL/SQLite，不得复制 provider wire normalization 或 domain schema。
- 不得把完整消息、reasoning 或伴随文本藏入 tool-call payload 或 metadata。

# 规范

- 使用现有 domain record/hash 校验；group 的完整性、顺序和 producer 必须显式校验。
- text、reasoning、tool call 分属各自 canonical semantic item；禁止旧单 item 接口 shim。
