# 目录用途

保存 rollout 当前结构及显式 assembly refs 18→19、状态版本引用19→20、消息版本20→21迁移及assembly共享内容21→22、view成员共享22→23、执行队列23→24、Skill选择次序24→25、view创建顺序25→26、执行调度与异步留言26→27，以及团队投递回执27→28结构。

```text
migrations/
├── rollout.sql       canonical 主结构
├── assembly/         请求引用、共享内容、计划与工具快照
├── checkpoint/       通道值与消息版本
├── context_sources/  来源控制状态、版本与索引
├── execution/        执行队列、调度唤醒与结果定位
├── catalog/          item、工具与资源引用索引
├── history/          历史 namespace、carrier 与有界查询
└── views/            view 顺序与成员共享
```

# 可修改内容

- 所属 owner 的静态 SQL 资产。

# 不可修改内容

- 不新增第二套 schema、动态生成 DDL 或静默回退。
- 不改动已登记迁移的文本 checksum、版本与业务约束。

# 规范

- Python 显式加载并协调执行顺序、事务、条件和参数，不扫描目录执行。
- `assembly/assembly_refs_018_019.sql` 保持原始 UTF-8 字节（含无末尾换行），以保留迁移 checksum。
- 按职责归组仅改变资源路径，不改变既有 SQL 名称、schema 版本、加载后的语句文本或有序迁移登记。
- 修改后验证建库、冷重开、失败回滚及对应 fork/activation 入口。
