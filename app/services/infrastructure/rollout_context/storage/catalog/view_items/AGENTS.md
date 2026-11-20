# 目录用途

维护 context view 的共享不可变成员块和有序引用；SQL view 是唯一逻辑成员查询入口。

# 可修改内容

- 成员块发布、克隆、追加、过滤；旧结构转换归 `storage/migrations/v22_sqlite_to_v23_sqlite.py`，复用此处原语。

# 不可修改内容

- 不保存 canonical 正文，不维护第二套成员集合，不实现业务选择策略。

# 规范

- 修改引用必须处于原 SQLite 事务；块不可改写，同 hash 异内容明确失败。
- 历史 view 的引用独立冻结，不依赖可变父 view；SQL 结构只在 `storage/migrations/` 中定义。
