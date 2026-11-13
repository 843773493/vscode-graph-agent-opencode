# 目录用途

Trace SQLite 的有序结构迁移。

## 可修改内容

- 新增随 schema 版本递增的 SQL 迁移。

## 不可修改内容

- 不引入 JSONL 双写、自动扫盘导入或业务流程。

## 规范

- 复用 SQLiteStateDatabase；正文与索引在同一事务中提交。
