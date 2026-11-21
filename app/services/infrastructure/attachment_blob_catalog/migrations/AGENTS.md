# 目录用途

保存附件 catalog 的 SQL 资产与已登记版本转换。

```text
migrations/
├── managed_directory_schema.sql     现行 schema 5 的唯一结构
├── upgrade_schema4_directories.sql   已有 schema 4 转换 SQL，保留原名和字节
├── v4_sqlite_to_v5_sqlite.py         原子转换协调，由 catalog 显式调用
└── schema3/                         离线维护核对用的旧结构、索引和触发器
```

# 可修改内容

- 所属 owner 的 SQL 资产与版本转换协调；schema 3 的跨资源备份、物化和安装仍归原 `migration/` 维护入口。

# 不可修改内容

- 不加入正常业务编排或独立迁移引擎，不把旧结构接回线上读写。
- 不保留 Python 内联副本或静默回退。

# 规范

- Python 显式加载并决定执行顺序、事务与参数绑定；不扫描目录执行。
- 保持已有结构、版本、约束和执行方式，修改后运行对应 owner 测试。
