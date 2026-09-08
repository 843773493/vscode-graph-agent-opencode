# 目录用途

跨 session legacy 坐标与 acceptance lineage。

# 可修改内容

- 仅修改 fork 提交过程中的目标本地化与一致性检查。

# 不可修改内容

- 不提供 v1 runtime fallback，不修改 source 原件，不建立第二 canonical writer。

# 规范

- source 坐标仅作审计；运行时引用必须指向 target namespace。
- 失败明确报错，保留 fork journal 的恢复边界。
