# 目录用途

`fork/` 负责 v2 跨 session fork 的 target-local identity、lineage 和保留策略适配。

# 可修改内容

- 可以实现 source 到 target 的 identity mapping、offset 审计坐标和 retention port。

# 不可修改内容

- 不得读取 v1 runtime、复制 source acceptance key 或成为 canonical item writer。

# 规范

- target 的 Turn/item/acceptance/execution/assembly/detail/overlay identity 必须在 target namespace 内唯一；source 只保留 lineage。
