# 目录用途

保存 Gateway 状态库的有序 SQL 迁移；文件编号与 `_GATEWAY_MIGRATIONS` 原版本对应。

# 可修改内容

- 由状态库 owner 显式登记的新迁移及 SQL 资产。
- `legacy_config/` 中由 Python 数据转换回调按原事务执行的迁移 SQL。

# 不可修改内容

- 不修改已发布迁移的文本和顺序，不在 SQL 中自行开启或提交事务。
- 不放工作区业务数据、连接配置或普通运行期查询。

# 规范

- UTF-8 编码，由 `app.core.sqlite_state.load_sql` 加载；Python 显式装配顺序。
- 016 为现有 Python 数据迁移回调，没有对应编号 SQL；不得补占或重编号。
