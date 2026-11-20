# 目录用途

保存所属 Session/导航存储 owner 的静态 SQLite 表、索引与触发器定义。
`navigation_queue/` 保存同库导航队列的3表/2索引，物理结构仍由core catalog统一初始化和校验。
`v11_sqlite_to_v12_sqlite.py` 只在跨资源停写维护中转换固定实验来源的已结算普通创建意图及 Provider 字段，完整原库备份由维护入口负责。
`v10_sqlite_to_v11_sqlite.py` 负责完整 v10 SQLite catalog 到 v11 的显式副本转换，复用同目录既有 SQL。
`nodes_navigation_revision_update_v10_v11.sql` 冻结固定来源 `cb90835523b9ff6d91e3f8dfaf3491855d55191d` 的旧 Provider 列触发器，仅供 v10/v11 离线校验；现行 schema 不加载它。

# 可修改内容

- 该 owner 显式登记的结构与迁移 SQL。
- 按两端版本和格式命名的 Python 转换及其原有校验、快照和事务协调。

# 不可修改内容

- SQL 资产不自行连接数据库或提交事务；Python 转换只协调本 owner 的离线升级，不复制正常业务逻辑。
- 不保留同一 DDL 的 Python 内联副本。

# 规范

- UTF-8 编码，由所属 Python 模块显式加载，执行方式及顺序保持原合同。
- 修改既有结构须遵守所属数据库的版本/拒绝策略，本目录不提供隐式升级。
- 显式转换入口为 `uv run python -m app.core.session_catalog_store.migrations.v10_sqlite_to_v11_sqlite --source <绝对路径> --destination <新绝对路径>`，原件不变，不自动安装副本或接管存活 owner；不保留旧模块转发入口。
