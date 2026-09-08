# 目录用途

`app/services/mapping/itemized/` 存放 v2 selection 到 LangChain/history DTO 的纯映射。

# 可修改内容

- 可以实现 CanonicalItem、ContextSelectionEntry 和 request-only contribution 的无 I/O projection。
- 可以记录 source identity、order 和 capability loss 到映射结果 metadata。

# 不可修改内容

- 不得扫描 RolloutStorage、JSONL、SQLite 或 detail store。
- 不得实现业务规则、provider wire bridge 或持久化。

# 规范

- 只接受 Saver 已提交的 plan/snapshot，按 `selection.plan_ordinal` 消费，不自行排序。
- 缺失 ref、hash/length 不匹配和未知 payload 必须抛出，不得静默丢失。
