# 目录用途

保存 rollout 当前结构及显式 assembly refs 18→19、状态版本引用19→20、消息版本20→21迁移及assembly共享内容21→22、view成员共享22→23、执行队列23→24、Skill选择次序24→25结构。

# 可修改内容

- 所属 owner 的静态 SQL 资产。

# 不可修改内容

- 不新增第二套 schema、动态生成 DDL 或静默回退。
- 不改动已登记迁移的文本 checksum、版本与业务约束。

# 规范

- Python 显式加载并协调执行顺序、事务、条件和参数，不扫描目录执行。
- `assembly_refs_018_019.sql` 保持原始 UTF-8 字节（含无末尾换行），以保留迁移 checksum。
- 修改后验证建库、冷重开、失败回滚及对应 fork/activation 入口。
