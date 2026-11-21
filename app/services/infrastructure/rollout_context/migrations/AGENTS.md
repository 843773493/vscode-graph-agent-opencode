# 目录用途

保存 rollout owner 的显式格式转换，按来源版本、来源格式、目标版本和目标格式查找。

## 可修改内容

- `v1_jsonl_to_v2_jsonl/`：已登记 v1 消息行到 v2 canonical item 的一次性转换、报告及显式 CLI。

## 不可修改内容

- 不作为正常 history/provider/checkpoint/runtime 的旧 reader，不建立双写或 fallback。
- 不在此复制 storage 的有序 SQL 或 schema；SQLite 结构仍归 `storage/migrations/`。

## 规范

- 一个转换涉及多个阶段时，以同一两端版本和格式命名为包，阶段模块留在包内，不合成巨型脚本。
- 当前入口为 `uv run python -m app.services.infrastructure.rollout_context.migrations.v1_jsonl_to_v2_jsonl`，不保留旧脚本或旧模块转发。
- 执行由显式入口发起，不扫描目录发现迁移；业务身份、audit 格式、校验与发布顺序保持原合同。
