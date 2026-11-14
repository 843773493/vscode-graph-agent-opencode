# 目录用途

工作区外部资源身份与 operation lease 的唯一账本。`ledger.py` 管理占用转换和发布顺序，`models.py` 定义持久事实，`database.py` 管理 SQLite 事务，`migrations/` 保存有序 DDL。

# 可修改内容

资源登记、租约转换、冷恢复、通知与持久事务。

# 不可修改内容

不代替实际 owner 判断外部操作是否结束，不删除幂等或恢复仍依赖的旧 lease，不恢复 JSON/SQLite 双读。

# 规范

- 被动列表观察支持完整核验后整批提交，与单资源更新共用状态规则，不结清或改写原 operation 占用。
- 普通操作只提交本次变化，成功后发布内存快照；冷恢复统一校验全部身份、状态及引用。
- 派发保护沿 SQLite FULL 同步事务提交，通知保持原事件循环和原提交顺序。
- 使用工作区 `.boxteam/resources.sqlite`，普通请求拒绝旧 JSON。用户明确批准正常升级仅备份后退休精确空 records／leases 的开发账本，由 `retire_empty_legacy_ledger` 在原 Workspace 停写锁内执行；非空或未知形状保留原件并要求决定，不双读、不推测完整 JSON 迁移。已有附件离线迁移仍按其明确旧输入契约读取 JSON。
- 不在普通请求中做全历史校验、裁剪或 VACUUM。
