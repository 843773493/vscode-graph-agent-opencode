# 目录用途

`app/gateway/config/` 是 Gateway 配置加载与热重载的同名子包（facade + 按垂直链路拆分的模块），
由原单文件 `app/gateway/config.py` 物理下线后拆入，导入路径 `app.gateway.config` 保持不变。

```text
app/gateway/config/
├── __init__.py                 facade：组装 ReloadService，并维护原公开导入出口
├── values.py                   配置值对象与无副作用的值解析
├── loader.py                   合并来源、校验配置、生成 GatewayConfig
├── sources.py                  来源层读取、迁移和来源明细
├── connection_ids.py           connection_id 规范化、迁移和回滚
├── reload_lifecycle.py         ReloadLifecycleMixin：watcher、事件游标与 reload
└── reload_pending_restart.py   ReloadPendingRestartMixin：pending restart 与 health proof
```

配置读取链：`sources.py` 提供持久来源，`connection_ids.py` 规范化旧配置，`loader.py`
完成合并与校验，`values.py` 定义返回的配置值。热重载链：`__init__.py` 组装公开服务，
`reload_lifecycle.py` 管理 watcher 和 reload 状态，`reload_pending_restart.py` 管理重启提交与健康证明。
新增职责先归入对应的垂直模块；只有出现独立 owner 和调用边界时才继续下沉目录。

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

