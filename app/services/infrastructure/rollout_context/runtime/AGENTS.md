# 目录用途

`runtime/` 负责实时 context ledger、detail store、composer、reconciliation 和 stream accumulator 的运行时适配。

# 可修改内容

- 可以维护内存 draft、source provenance、cache-preserving overlay 和受保护 detail port。
- 可以调用 Saver 提供的提交快照接口。

# 不可修改内容

- 不得把内存 view 当作 canonical JSONL/SQLite 事实源。
- 不得直接读取 v1、LangChain history 或 Provider wire。

# 规范

- source revision、hash、length、ordinal 和 loss 必须显式记录；失败不得静默降级。
