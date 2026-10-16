# 目录用途

fork 协调 journal、来源保留和目标删除的持久化 owner。

# 可修改内容

- 可以修改冻结快照的物化提交、恢复、精确来源 claim 和目标删除清理。

# 不可修改内容

- 不提供 v1 runtime fallback，不修改 source 原件，不建立第二 canonical writer。

# 规范

- 当前物理 owner 与历史执行 scope 分开；历史 ID、状态和 bytes 保持原样。
- prepared 由原冻结 operation 重放，target_committed 只收敛当前目标 journal。
- 失败明确报错，保留 fork journal 的恢复边界。
