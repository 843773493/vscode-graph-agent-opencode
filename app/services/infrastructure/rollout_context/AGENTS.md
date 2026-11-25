# 目录用途

`app/services/infrastructure/rollout_context/` 是 rollout context 的 I/O owner，连接 domain、session path resolver、SQLite/JSONL 和受保护 detail。

# 可修改内容

- 可以实现当前格式的 storage、recovery、assembly、detail、fork、有序 SQLite migration 与 provider request bridge。
- 可以调用受控会话路径解析器和外部 I/O port。

# 不可修改内容

- 不得复制 domain schema/hash，不得实现 LangChain/history 纯映射。
- 未发布 v1/v2 canonical 转换已退出支持，不保留旧 reader/importer；不得创建双写、双 projector 或第二事实源。

# 规范

- 失败必须暴露 source-mismatch、detail-unavailable 或 recovery_required 等明确错误。
- 所有新 source 子目录都必须有自己的四段式 `AGENTS.md`，并保持依赖只指向 domain/port。
