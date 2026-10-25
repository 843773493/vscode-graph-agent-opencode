# 目录用途

存放核心文件持久化与 SQLite 基础能力的单元测试。

```text
storage/
├── test_atomic_fs.py        原子文件替换与 durability 顺序
├── test_file_permissions.py 跨平台私有文件权限
├── test_sql_assets.py       SQLite SQL 资源定位和缺失行为
├── test_sqlite_state.py     数据库 owner、迁移与连接行为
├── test_sqlite_vfs.py       当前 Python SQLite VFS 路径预算
└── test_sqlite_errors.py    运行时失败事实、原始诊断与异常类型
```

# 可修改内容

- `app/core/atomic_fs.py`、`app/core/file_permissions.py` 与 `app/core/sqlite_state.py` 的底层持久化测试。
- 这些测试使用的临时数据库、临时文件和进程替身。

# 不可修改内容

- 不在此放置具体业务存储模块的表结构或服务行为测试。
- 不访问真实用户目录、默认工作区或项目根目录中的运行时状态。

# 规范

- 每个测试文件的工作区和产物分别镜像写入
  `out/tests/unit/core/storage/<测试名>/{workspace,artifacts}/`。
- SQLite 路径测试必须检查实际 Python SQLite 运行时，不能以另一份系统 SQLite 代替。
- 故障与并发测试保留能区分正确持久化顺序和错误实现的断言。
