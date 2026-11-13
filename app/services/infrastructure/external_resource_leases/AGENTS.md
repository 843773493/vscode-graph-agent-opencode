# 目录用途

工作区外部资源身份与 operation lease 的唯一账本。`ledger.py` 管理占用转换和发布顺序，`models.py` 定义持久事实，`database.py` 管理 SQLite 事务，`migrations/` 保存有序 DDL。

# 可修改内容

资源登记、租约转换、冷恢复、通知与持久事务。

# 不可修改内容

不代替实际 owner 判断外部操作是否结束，不删除幂等或恢复仍依赖的旧 lease，不恢复 JSON/SQLite 双读。

# 规范

- 普通操作只提交本次变化，成功后发布内存快照；冷恢复统一校验全部身份、状态及引用。
- 派发保护沿 SQLite FULL 同步事务提交，通知保持原事件循环和原提交顺序。
- 使用工作区 `.boxteam/resources.sqlite`，拒绝旧 JSON；没有 release 基线时不推测自动迁移。已有附件离线迁移仍按其明确旧输入契约读取 JSON。
- 不在普通请求中做全历史校验、裁剪或 VACUUM。
