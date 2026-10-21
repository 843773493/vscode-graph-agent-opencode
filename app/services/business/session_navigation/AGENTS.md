# 目录用途

实现工作区会话目录、文件夹稳定引用、分页搜索和 breadcrumb 业务规则。

# 可修改内容

- 权威会话索引的目录投影、revision、分页、搜索以及受控移动服务。
- Session 稳定 ID、冻结 locator 的路径解析，以及 SQLite-only Folder 的导航投影规则。
- 异步 mutation 队列持久层：`queue_store.py` 门面保留队列语义方法与 `NavigationMutationQueueStore` 类名，`queue_schema.py` 承载两张旁挂表的物理 schema 与幂等建表，`queue_records.py` 承载行投影与跨进程幂等 preimage，`queue_events.py` 承载事件 outbox 链，`queue_dependencies.py` 承载依赖解析与失败传播。

# 不可修改内容

- 不实现 Gateway 跨工作区组织或生成器调度。
- 不另写与统一权威索引并行的缓存或 JSON 文件夹关系。
- 不允许业务调用方直接假设或拼接 `.boxteam/sessions/{session_id}/` 固定路径。
- 不得改动 `compute_intent_preimage_hash` 的字段集与字节序，也不得把 `_next_queue_seq`/`_next_event_seq` 的序号分配改成多次查询；并发幂等与序号单调是不变量。
- 不得改动调用方决定的事务边界：队列持久层所有写方法只接受调用方的 `sqlite3.Connection`，不得自行 commit 或另开写事务。

# 规范

- 静态结构 SQL 位于同包 `sql/`，所属 Python 模块只加载和协调原执行顺序；SQL 资产不自行提交事务，不保留内联结构副本。

- 权威 SQLite catalog 保存稳定 ID、显示名、导航父链和 Session 冻结 locator；Folder 无物理目录或 manifest，`session.json` 只保存不可变创建 metadata。
- `parent_session_id` 只从 catalog 最近祖先 Session 派生，不回写 manifest；fork、delegation、上下文来源和生成器 lineage 与导航父链分离。
- Session 的物理 locator 为创建 intent 冻结的 UTC 日期桶 `sessions/YYYY/MM/DD/{session_id}`；导航子节点只由 catalog 父链组织，不创建物理 `children/` 树。生成器使用同一创建 owner 建立节点并指定逻辑父节点。
- 损坏、重复或循环关系必须抛出详细错误。
- 移动、重命名只更新 catalog，不搬目录或改变 locator；创建、删除由对应 owner 协调 catalog 与物理存储。所有路径读取经统一解析器，手工目录漂移必须向 API/UI 暴露错误。
- `from app.services.business.session_navigation.queue_store import X` 的外部导入路径与导出名保持稳定，拆分产物不得反向依赖门面。
