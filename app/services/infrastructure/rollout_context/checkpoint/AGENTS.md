# 目录用途

`checkpoint/` 负责 v2 checkpoint persistence、view projection、fork 和 compaction adapter。

# 可修改内容

- 可以实现 checkpoint/view/branch 的 v2 adapter 和已提交 item anchor 操作。
- 可以调用 rollout storage 与 domain identity。

# 不可修改内容

- 不得实现 v1 runtime fallback、Provider normalization 或 LangChain 纯映射。
- 不得把 checkpoint message projection 作为 canonical item 事实源。

# 规范

- durable 操作绑定已提交 anchor，history-only 变化不得隐式物化 source overlay。
