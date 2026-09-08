# 目录用途

仅在显式 SQLite schema3→4 升级中导入既有 sealed plan registry。

# 可修改内容

- 源 schema3 严格预检、确定性 SQL、来源指纹和提交前验证。
- 原 plan 与 assembly 一对一的无正文导入。

# 不可修改内容

- 不修改源连接、canonical JSONL、snapshot、detail 文件或既有 identity。
- 不为正常 runtime 提供 schema3 fallback，不创建或补造 draft 历史。
- 不持有 schema DDL、事务提交、备份恢复或磁盘发布的第二个 owner。

# 规范

- 调用方持有 rollout owner 写锁，源连接必须无写事务；prepare 只在内存副本演练。
- DDL 来自 storage schema owner；SQL 绑定源业务指纹及全部 import provenance。
- 同 plan 多 assembly、缺失工具行、未知来源与损坏 manifest 一律拒绝，保留原件。
- verify_migrated 只读且允许事务内调用，不提交、不写审计文件。
