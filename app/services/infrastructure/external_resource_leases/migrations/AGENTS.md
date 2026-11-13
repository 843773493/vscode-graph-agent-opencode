# 目录用途

外部资源租约 SQLite 的静态有序迁移。

# 可修改内容

由账本 owner 显式登记的 schema SQL。

# 不可修改内容

不复制业务状态转换，不扫描文件推导迁移顺序。

# 规范

DDL 由 `database.py` 通过现有 SQLiteStateDatabase 执行；没有 release 依据时不增加开发 JSON 自动迁移。
