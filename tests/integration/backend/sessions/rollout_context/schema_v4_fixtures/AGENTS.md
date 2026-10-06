# 目录用途

保留真实 schema3 SQLite DDL，供旧实验 v2 的明确拒绝与零改写验收。

# 可修改内容

- 历史 DDL 的来源说明与拒绝合同所需资源说明。

# 不可修改内容

- 不改写 frozen schema3.sql，不用当前 schema 初始化后改版号伪造历史结构。
- 不保存运行数据库、正文或 Provider 密钥，不实现历史升级 reader。

# 规范

- schema3.sql 来自 schema3 激活期间的真实 SQLite sqlite_master，仅作只读负向输入。
- 正式测试数据库与可复查产物写入对应测试的 out/tests 工作区。
