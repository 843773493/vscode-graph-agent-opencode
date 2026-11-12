# 目录用途

存放 Gateway 全局控制面扩展，负责工作区导航组织、全局控制面配置和可重建目录镜像。

## 子包索引

- `gateway_state/`：Gateway 全局状态库 `gateway-state.sqlite` 基础设施 `GatewayStateStore` 的唯一实现点（facade 在 `__init__.py`，保留类声明、连接生命周期、KV 读写与模块级符号再导出）。其下按垂直链路分 mixin：legacy 秘密迁移（`lifecycle.py`）、active snapshot 与 pending candidate（`active_snapshot.py`）、active 提升（`apply_promotion.py`）、config apply journal（`apply_journal.py`）、config apply claim（`apply_claim.py`）、gateway restart intent（`restart_intent.py`）、restart apply 与失败记录（`restart_apply.py`），schema 迁移序列与一次性镜像行迁移由 `_schema.py` 协调，静态 SQL 位于同包 `migrations/`。`_GATEWAY_MIGRATIONS` 的条目顺序即 `SQLiteStateDatabase.schema_version` 的权威来源，不得改动。
- `gateway_registry/`：Gateway 全局 workspace registry 与 registry apply journal 垂直链路（`GatewayRegistryMixin`）。
- `gateway_config_source/`：Gateway config source layer、source journal 与 generation 水位垂直链路（`GatewayConfigSourceMixin`）。
- `gateway_config_events/`：Gateway 全局 config event outbox 与 consumer/relay 投递账本垂直链路（`GatewayConfigEventMixin`）。
- `gateway_runtime_generation/`：Gateway runtime generation 生命周期与健康证明垂直链路（`GatewayRuntimeGenerationMixin`）。
- `catalog_search.py`：保留原后台目录同步、配置健康与 owner-v3 派生镜像；不提供聚合搜索或查询时同步。离线联邦仅复用原 catalog owner 的纯查询。
- `workspace_discovery.py`：按原 registry／navigation 元数据提供有界工作区发现；返回完整 Gateway／工作区 UUID owner，不生成另一工作区路由身份，不探测健康、启动目标或读取工作区数据。
- `diagnostic_logs.py`：复用原 diagnostics 来源提供有界正文查询与原行定位；工作区仅筛选关联受管文件，共享进程按文件去重，Gateway 自有来源须明确选择，不成为普通资源或聊天来源。
- `diagnostic_log_transport.py`：原诊断管理的目标路由、联邦凭据与有界响应校验；搜索队列和具名管理入口共用，不实现正文匹配或另建调度。
- 联邦搜索范围与跨目标查询编排归 `../search/`，不在目录镜像或控制面路由中另建调度器。

# 可修改内容

- `/api/gateway/*` 下的工作区导航、显式旧生成器迁移和全局目录查询接口。
- Gateway 全局状态目录中的原子配置存储与派生索引。
- 可以为上述子包补充目录索引与职责边界说明；新增垂直链路时同步在此登记。
- `navigation.py` 的 `read_nodes`／`validate_nodes` 是普通导航和身份维护预检共用的只读快照入口；预检不得调用会同步并保存节点的 `list_tree`，不得删除无法映射的原引用。

# 不可修改内容

- 不直接读写任意工作区的 `.boxteam/` 业务数据。
- 不实现 Agent、Job、消息或会话创建业务；实际执行必须代理到目标工作区后端。
- 不得改动 `gateway_state/_schema.py` 中 `_GATEWAY_MIGRATIONS` 的条目顺序或内容。

# 规范

- 权威配置和可重建索引必须明确分离。
- 跨工作区会话引用必须同时包含 `workspace_id` 和 `session_id`。
- 写入失败必须抛出明确错误，配置文件使用原子替换。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

`generator_import.py` 在显式管理请求中读取旧文件快照、备份并通过目标工作区 API 移交；`read_session_generator_inputs` 是显式导入与身份维护预检共用的只读校验入口，核验原 bytes、冻结 `source_hash`、精确回执及运行源退休状态。身份预检不得自动导入或改写历史备份；正常启动不读取旧生成器，不保存新任务或调度状态。离线目标保留源文件并明确报告，不能改绑默认工作区。

迁移安装前须核验完整备份与原件，目标接受后再核验全部文件才能开始退休。快照与回执复用文件及目录同步的原子写；删除原件后同步对应目录，断点重试不得重新发布已有回执。
