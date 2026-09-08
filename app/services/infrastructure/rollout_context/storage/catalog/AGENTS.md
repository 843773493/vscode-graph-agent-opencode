# 目录用途

承载 v2 canonical catalog 与其 SQLite 派生索引的存取和完整性边界。

# 可修改内容

- item catalog、消息与 Turn 索引的 SQL 查询和写入。
- 基于不可变 JSONL 的 catalog 完整性校验。

# 不可修改内容

- 不得实现 LangChain、Provider wire 或 UI DTO 投影。
- 不得自动修复已提交 catalog，或保存第二份 canonical 正文。

# 规范

- catalog 与提交链及 JSONL 必须一致；损坏时明确报错。
- 摘要读取只访问 SQLite，不打开 JSONL 正文。
