# 目录用途

存放 Gateway 全局控制面扩展，负责工作区导航组织、会话生成器配置、生成运行账本和可重建目录镜像。

## 子包索引

- `gateway_state/`：Gateway 全局状态库 `gateway-state.sqlite` 基础设施 `GatewayStateStore` 的唯一实现点（facade 在 `__init__.py`，保留类声明、连接生命周期、KV 读写与模块级符号再导出）。其下按垂直链路分 mixin：legacy 秘密迁移（`lifecycle.py`）、active snapshot 与 pending candidate（`active_snapshot.py`）、active 提升（`apply_promotion.py`）、config apply journal（`apply_journal.py`）、config apply claim（`apply_claim.py`）、gateway restart intent（`restart_intent.py`）、restart apply 与失败记录（`restart_apply.py`），schema 迁移序列与一次性镜像行迁移收敛在 `_schema.py`。`_GATEWAY_MIGRATIONS` 的条目顺序即 `SQLiteStateDatabase.schema_version` 的权威来源，不得改动。
- `gateway_registry/`：Gateway 全局 workspace registry 与 registry apply journal 垂直链路（`GatewayRegistryMixin`）。
- `gateway_config_source/`：Gateway config source layer、source journal 与 generation 水位垂直链路（`GatewayConfigSourceMixin`）。
- `gateway_config_events/`：Gateway 全局 config event outbox 与 consumer/relay 投递账本垂直链路（`GatewayConfigEventMixin`）。
- `gateway_runtime_generation/`：Gateway runtime generation 生命周期与健康证明垂直链路（`GatewayRuntimeGenerationMixin`）。

# 可修改内容

- `/api/gateway/*` 下的工作区导航、生成器和全局目录查询接口。
- Gateway 全局状态目录中的原子配置存储与派生索引。
- 可以为上述子包补充目录索引与职责边界说明；新增垂直链路时同步在此登记。

# 不可修改内容

- 不直接读写任意工作区的 `.boxteam/` 业务数据。
- 不实现 Agent、Job、消息或会话创建业务；实际执行必须代理到目标工作区后端。
- 不得改动 `gateway_state/_schema.py` 中 `_GATEWAY_MIGRATIONS` 的条目顺序或内容。

# 规范

- 权威配置和可重建索引必须明确分离。
- 跨工作区会话引用必须同时包含 `workspace_id` 和 `session_id`。
- 写入失败必须抛出明确错误，配置文件使用原子替换。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
