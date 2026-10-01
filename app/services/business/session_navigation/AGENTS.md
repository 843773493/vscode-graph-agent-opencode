# 目录用途

实现工作区会话目录、文件夹稳定引用、分页搜索和 breadcrumb 业务规则。

# 可修改内容

- 权威会话索引的目录投影、revision、分页、搜索以及受控移动服务。
- session manifest、folder manifest 与稳定 ID 的路径解析规则。
- 异步 mutation 队列持久层：`queue_store.py` 门面保留队列语义方法与 `NavigationMutationQueueStore` 类名，`queue_schema.py` 承载两张旁挂表的物理 schema 与幂等建表，`queue_records.py` 承载行投影与跨进程幂等 preimage，`queue_events.py` 承载事件 outbox 链，`queue_dependencies.py` 承载依赖解析与失败传播。

# 不可修改内容

- 不实现 Gateway 跨工作区组织或生成器调度。
- 不另写与统一权威索引并行的缓存或 JSON 文件夹关系。
- 不允许业务调用方直接假设或拼接 `.boxteam/sessions/{session_id}/` 固定路径。
- 不得改动 `compute_intent_preimage_hash` 的字段集与字节序，也不得把 `_next_queue_seq`/`_next_event_seq` 的序号分配改成多次查询；并发幂等与序号单调是不变量。
- 不得改动调用方决定的事务边界：队列持久层所有写方法只接受调用方的 `sqlite3.Connection`，不得自行 commit 或另开写事务。

# 规范

- 权威索引保存稳定 ID、显示名和父子关系；folder/session manifest 与 ID 命名目录是受索引校验的物理存储。
- `parent_session_id` 表达同一工作区中的父会话，必须与权威索引中最近的祖先会话一致；上下文来源使用独立字段表达。
- 会话只允许通过保留的 `children/` 边界包含子会话和组织文件夹；生成器挂载到会话时以该边界作为输出路径基准。
- 损坏、重复或循环关系必须抛出详细错误。
- 移动、重命名和删除必须经统一解析器同步更新权威索引与真实目录；手工目录漂移必须向 API/UI 暴露错误。
- `from app.services.business.session_navigation.queue_store import X` 的外部导入路径与导出名保持稳定，拆分产物不得反向依赖门面。

