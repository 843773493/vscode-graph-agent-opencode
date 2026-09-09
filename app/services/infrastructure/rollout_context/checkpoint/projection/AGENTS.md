# 目录用途

`projection/` 负责把已提交 canonical item 和 SQLite locator 投影为 checkpoint/history 所需的 LangChain message 视图。

# 可修改内容

- 可以调用注入的 MessageCodec、已提交 item reader 和 SQLite 派生索引。
- 可以维护 message/tool/reasoning 的 checkpoint 派生 projection。

# 不可修改内容

- 不得把 message projection 作为 canonical item 事实源。
- 不得在此实现 Provider wire、v1 runtime fallback 或自行扫描 JSONL 猜测历史。

# 规范

- 纯 LangChain 编解码只由注入 codec 完成；本目录只协调有界读取和派生 projection。
- 所有 locator 必须来自已验证的 SQLite commit/catalog 边界。
