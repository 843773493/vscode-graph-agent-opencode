# 目录用途

`app/gateway/config/` 是 Gateway 配置加载与热重载的同名子包（facade + 按垂直链路拆分的模块），
由原单文件 `app/gateway/config.py` 物理下线后拆入，导入路径 `app.gateway.config` 保持不变。

- `values.py`：配置值对象（`GatewayConfig`/`ConfiguredRemoteGateway`/`ConfiguredTheme`）与
  取值、路径解析辅助（`resolve_gateway_path` 等）。
- `connection_ids.py`：connection_id 规范化、迁移与回滚。
- `sources.py`：配置来源层加载/迁移/日志与来源明细。
- `loader.py`：`load_gateway_config` 与 consumer 健康摘要校验。
- `reload_lifecycle.py`：`ReloadLifecycleMixin`（服务初始化、状态、事件游标、watcher、reload）。
- `reload_pending_restart.py`：`ReloadPendingRestartMixin`（pending restart 与 health proof）。
- `__init__.py`：facade，组装 `GatewayConfigReloadService` 并再导出全部原顶层符号。

# 可修改内容

- 各垂直链路的实现模块与 facade 的再导出清单。
- 新增子目录时必须补充自己的 `AGENTS.md`。

# 不可修改内容

- 不得回退 `load_gateway_config` 的 schema 解析与校验逻辑（含 `schema_path` 兜底）。
- 不得改变 `app.gateway.config` 对外暴露的符号与属性访问契约（含原模块级导入名）。
- 不得把 `_GATEWAY_SOURCE_LAYER_AUTHORITY` 改为从 workspace 侧 import 共享表。
- 不得引入 `__file__`/`parents` 向上推导仓库根的路径解析；路径一律基于显式传入的
  绝对路径或 `get_user_config_root()` 等既有解析器。

# 规范

- 拆分为纯搬迁：函数体逐字保留，不做语义改写。
- 新增模块必须保持每文件 ≤800 行。
- 失败直接抛出明确错误，不得静默降级；不得返回虚假默认值。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

