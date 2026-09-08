# 目录用途

冻结显式 schema3→4 升级使用的真实 schema3 SQLite DDL。

# 可修改内容

- 历史 DDL fixture 的来源说明和严格结构断言所需资源。

# 不可修改内容

- 不调用当前 schema 初始化并仅改版号伪造旧库；不从 out/tests 动态加载 fixture。
- 不保存测试运行数据库、正文或 Provider 密钥。

# 规范

- schema3.sql 来自 schema3 激活期间通过验收的真实 SQLite sqlite_master。
- fixture 只读，正式数据库/备份写入对应测试的 out/tests 工作区。
