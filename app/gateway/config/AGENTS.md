# 目录用途

`app/gateway/config/` 是 Gateway 配置加载与热重载的同名子包（facade + 按垂直链路拆分的模块），
由原单文件 `app/gateway/config.py` 物理下线后拆入，导入路径 `app.gateway.config` 保持不变。

```text
app/gateway/config/
├── __init__.py                 facade：组装 ReloadService，并维护原公开导入出口
├── values.py                   配置值对象与无副作用的值解析
├── loader.py                   合并来源、校验配置、生成 GatewayConfig
├── sources.py                  来源层读取、迁移、来源明细及启动失败记录
├── connection_ids.py           connection_id 规范化与当前配置新连接登记
├── reload_lifecycle.py         ReloadLifecycleMixin：watcher、事件游标与 reload
├── source_edit.py              GatewaySourceEditMixin：来源正文、纯预检与原 pending 保存
└── reload_pending_restart.py   ReloadPendingRestartMixin：pending restart 与 health proof
```

配置读取链：`sources.py` 提供持久来源，`connection_ids.py` 规范化当前配置，`loader.py`
完成合并与校验，`values.py` 定义返回的配置值。热重载链：`__init__.py` 组装公开服务，
`reload_lifecycle.py` 在唯一 reload 锁内管理 watcher 和 reload 状态，`source_edit.py` 将文件 CAS
与来源提交绑定到同一 pending/claim 链，`reload_pending_restart.py` 管理重启提交与健康证明。
旧版 connection_id 配置由 `configs.boxteam migrate-runtime-config` 显式迁移；普通 Gateway
加载遇到缺少身份的 v1 配置时明确失败。新增 v4 连接仍可由 loader 生成并登记稳定 ID。
新增职责先归入对应的垂直模块；只有出现独立 owner 和调用边界时才继续下沉目录。

# 可修改内容

- 各垂直链路的实现模块与 facade 的再导出清单。
- 新增子目录时必须补充自己的 `AGENTS.md`。

# 不可修改内容

- 不得回退 `load_gateway_config` 的 schema 解析与校验逻辑（含 `schema_path` 兜底）。
- 不得改变 `load_gateway_config`、`GatewayConfig` 等对外配置契约；旧 connection_id 自动迁移与回滚入口已由显式配置维护命令统一取代。
- 不得把 `_GATEWAY_SOURCE_LAYER_AUTHORITY` 改为从 workspace 侧 import 共享表。
- 不得引入 `__file__`/`parents` 向上推导仓库根的路径解析；路径一律基于显式传入的
  绝对路径或 `get_user_config_root()` 等既有解析器。

# 规范

- 拆分为纯搬迁：函数体逐字保留，不做语义改写。
- 新增模块必须保持每文件 ≤800 行。
- 失败直接抛出明确错误，不得静默降级；不得返回虚假默认值。
- watcher 的单次 reload 失败须保留原失败状态并记录异常，继续观察后续文件修复；显式 reload 仍抛错，取消继续向上传播，不增加第二套重试链。
- 失败事实与完整异常诊断复用 `app/core/failures.py`，不另存原生异常正文。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

