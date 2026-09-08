# 目录用途

维护 v2 registry、unsealed plan 与 sealed assembly 的纯领域序列化边界。

# 可修改内容

- 严格字段解析、类型检查与领域对象恢复。
- 草稿和封存快照共用的 registry parser。

# 不可修改内容

- 不得读取 SQLite、JSONL、详情文件、Provider 或 legacy artifact。
- 不得补造缺失的 owner、assembly binding、正文或 hash。

# 规范

- 草稿恢复不得创建 selection；封存恢复不得放松 manifest 必填约束。
- 同一字段只保留一个 parser；非法字段、未知版本和 owner 冲突必须明确报错。
