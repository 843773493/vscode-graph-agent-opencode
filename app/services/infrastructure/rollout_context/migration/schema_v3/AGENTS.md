# 目录用途

只在显式 SQLite schema2→3 升级入口解析旧 v2 detail/assembly artifact。

# 可修改内容

- 旧 artifact 严格解析、typed identity 映射、暂存发布、SQL 计划和备份审计。
- 保留原件的失败/崩溃重试与提交后核验。

# 不可修改内容

- 不修改 canonical JSONL、locator、schema owner 或 Saver。
- 不被普通 runtime/read 导入，不持有或访问 backend 私有密钥。

# 规范

- 调用方必须持有 rollout owner 排他锁；本模块不开始/提交源 SQLite 事务。
- prepare 全部预检通过后才写备份；publish 排他创建新叶文件，旧文件永不覆盖。
- 当前程序可以支持更高 schema；此模块的输入/输出固定为 2→3。
