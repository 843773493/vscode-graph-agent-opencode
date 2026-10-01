# 目录用途

`boundary/` 是 checkpoint 侧的边界闭合 owner：tool protocol 唯一公共 validator，以及
compaction、fork/history-prefix 与 rewind 操作的 typed adapter。它只做边界判定与委托，
不实现 checkpoint 持久化本身。

# 可修改内容

- 可以维护 tool protocol 闭合判定（安全 cutoff/anchor 与冲突码）的唯一实现。
- 可以维护 compaction/fork/operations 边界的 typed port 与 owner mixin 委托。

# 不可修改内容

- 不得复制第二份 tool protocol validator 或在别处重算安全 anchor。
- 不得直接读写 JSONL/SQLite/detail 文件，也不得实现 v1 fallback。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- 边界不合法必须显式抛带冲突码的异常，禁止静默降级或猜测。
- validator 全仓唯一；适配器只能委托它，不得内联第二套判定。

