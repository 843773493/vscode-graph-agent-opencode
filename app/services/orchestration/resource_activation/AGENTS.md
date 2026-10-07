# 目录用途

存放资源上下文激活的 Coordinator 与持久化 port，把 ResourceRegistry 已发布快照冻结到真实 Execution 或 ModelCall 粒度；配置边界仍为 turn|model_call。

# 可修改内容

- 有效配置到领域冻结 policy 值的构造、受保护 body port 和唯一 Saver port；不另定义领域 snapshot 类型。
- ResourceActivationCoordinator 的冻结、parent 关联和 registry generation 捕获逻辑。

# 不可修改内容

- 不得直接读文件、目录、网络、Gateway 或内存业务状态；只能消费 ResourceRegistry published snapshot。
- 不得建立第二 ContextStore/JSONL/SQLite writer，不得解析 provider locator 或 credential。

# 规范

- policy revision/hash 使用 JCS；binding 必须记录 effective boundary 和 source lineage 校验引用。
- sealed snapshot 不可变；同用户 Turn retry/resume 及同一 internal execution 复用完整冻结 policy，ModelCall 只能复用 parent execution 的 turn-bound binding。
