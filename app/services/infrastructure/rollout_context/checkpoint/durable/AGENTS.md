# 目录用途

`durable/` 是 v2 checkpoint/view/anchor 的 durable commit owner：checkpoint 索引、
active view 与已提交 item anchor 的持久化。它只负责提交/恢复，不做 history 投影。

# 可修改内容

- 可以维护 checkpoint/view/anchor 的 v2 原子提交与重启恢复。
- 可以维护 JSONL 已提交 offset 与 SQLite checkpoint 索引的一致性。

# 不可修改内容

- 不得实现 LangChain/Provider 映射或纯 history 投影。
- 不得把投影结果当作 canonical item 事实源，也不得保留 v1 运行路径。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- durable 操作必须绑定已提交 anchor；history-only 变化不得隐式物化 source overlay。
- 提交边界必须遵守 JSONL fsync 后 SQLite terminal convergence，失败必须显式抛出。

