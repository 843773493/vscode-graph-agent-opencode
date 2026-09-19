# 目录用途

存放资源上下文激活边界的 typed 快照与 Coordinator，把 ResourceRegistry 已发布快照冻结到 Turn 或 ModelCall 粒度。

# 可修改内容

- ResourceActivationPolicySnapshot、TurnResourceSnapshot、ModelCallResourceSnapshot 和唯一 Saver port。
- ResourceActivationCoordinator 的冻结、parent 关联和 registry generation 捕获逻辑。

# 不可修改内容

- 不得直接读文件、目录、网络、Gateway 或内存业务状态；只能消费 ResourceRegistry published snapshot。
- 不得建立第二 ContextStore/JSONL/SQLite writer，不得解析 provider locator 或 credential。

# 规范

- policy revision/hash 使用 JCS；binding 必须记录 effective boundary 和 source lineage 校验引用。
- sealed snapshot 不可变；Turn 内 policy 不得漂移，ModelCall 只能复用 parent 的 turn-bound binding。
