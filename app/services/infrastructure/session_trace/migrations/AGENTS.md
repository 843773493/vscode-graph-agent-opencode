# 目录用途

Trace SQLite 的有序结构迁移与有明确来源的格式转换。

## 可修改内容

- 新增随 schema 版本递增的 SQL 迁移。
- `v1_jsonl_to_v3_sqlite.py` 只转换固定来源 `bf0f2e1d9d037efcb0210c458fa157fb4d54a27a` 的完整 v1 JSONL 提交合同，校验完整旧提交合同；无法补齐新事实的旧 job_started／goal_cleared 经授权丢弃并报告数量，其余写入现行 envelope 并重算逻辑偏移。原字节保存在维护备份，旧游标不保证继续有效。

## 不可修改内容

- 不引入 JSONL 双写、扫盘猜测来源、尾部修复或业务流程；转换只由停写后的维护入口协调。

## 规范

- 复用 SQLiteStateDatabase；正文与索引在同一事务中提交。
