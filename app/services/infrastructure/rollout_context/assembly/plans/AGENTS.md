# 目录用途

负责 plan-scoped draft registry 的创建、修订、恢复校验和封存事务绑定。

# 可修改内容

- 可以实现使用 storage owner 提供的 SQLite connection 的 plan 生命周期。
- 可以调用 domain 严格解析和 JCS hash，维护 draft 与 sealed manifest 的一致性。

# 不可修改内容

- 不得创建连接、写 canonical JSONL、读取 Provider/源文件或实现第二个 detail store。
- 不得在正常读写中补表、回填缺失 registry 或导入 legacy artifact。

# 规范

- 事务与提交只由 Saver/storage owner 发起；本目录的写操作必须在该事务内。
- draft 不持有 selection/最终 detail/assembly ordinal；敏感和普通贡献正文均不复制入 registry。
- sealed plan 只能只读；损坏与幂等冲突必须显式报错，不重新猜测源数据。
- fork 保留原 scope 的真实 creation/draft/revision 与封存 hash，通过目标本地历史 binding 只读恢复；不得重造导入 variant 或修订历史记录。
