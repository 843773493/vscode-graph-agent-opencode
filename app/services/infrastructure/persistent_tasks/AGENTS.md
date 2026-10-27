# 目录用途

存放持久任务定义、不可变定义版本和运行账本的 SQL 存取，复用唯一 WorkspaceStateStore。

# 可修改内容

任务及运行的事务、revision、幂等和恢复索引读取。

# 不可修改内容

不另建数据库或迁移引擎，不调度模型和脚本，不写 canonical 上下文正文。

# 规范

DDL 归 WorkspaceStateStore 的 SQL 资产；运行只引用冻结定义版本，来源正文归既有 source owner。冲突与损坏明确报错。
