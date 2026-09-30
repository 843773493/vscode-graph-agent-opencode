# 目录用途

`app/services/infrastructure/config_service/` 是工作区配置服务 `ConfigService` 的
唯一实现点（原先的 `config_service.py` 单文件已拆分入本包）。facade 落在本包
`__init__.py`，只保留 `ConfigService` 的类声明、`__init__`、极少量直通访问器
（`_get_effective_config`、`get`）与 5 个类常量；其余 100+ 方法按族逐字搬迁到同包各
mixin，由 `ConfigService` 多继承装配并共享同一 `self` 与 `__init__` 槽位。

本包承载的族：source-layer 读取与来源权威表（`config_source_layers.py`）、snapshot
构建与 pending-restart 契约（`config_snapshot.py`）、事件游标与标量访问器
（`config_runtime_accessors.py`）、reload 与 shadow 生命周期（`config_reload.py`）、
公开配置与 provider/日志（`config_public.py`）、agent 身份与默认值
（`config_agents.py`）、agent 工具策略与 MCP（`config_agent_tools.py`），以及共享模块级
符号的唯一定义点 `config_service_common.py`。

# 可修改内容

- 可以维护本包 facade `__init__.py` 中的 `ConfigService` 类声明、`__init__`、直通
  访问器、5 个类常量与 `__all__`。
- 可以维护各方法族 mixin 文件中的方法体与族内私有静态方法。
- 可以维护 `config_service_common.py` 中的 `logger`、`ConfigCandidateApplier`、
  `_INLINE_SOURCE_KEY`、`_SOURCE_LAYER_AUTHORITY` 与 `release_inline_config_vrn_for_file`。
- 可以维护随本包落地的四段式 `AGENTS.md`。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/services/infrastructure/` 下。

# 不可修改内容

- 不得改变对外契约：`ConfigService` 类名、构造签名、公开方法名与语义、模块导入路径
  `app.services.infrastructure.config_service.ConfigService`，以及 `__init__.py` 对
  `release_inline_config_vrn_for_file` 的再导出，均 MUST 保持不变。
- 不得为兼容旧调用点保留转发方法、旧模块 shim 或双套实现；跨族协作必须走同一 `self`。
- 不得在 `config_service_common.py` 之外重复定义 `logger`、`ConfigCandidateApplier`、
  `_SOURCE_LAYER_AUTHORITY` 或 `release_inline_config_vrn_for_file`；其它族一律从本模块导入。
- 不得静默改动搬迁方法的异常类型、错误消息或注释；搬迁必须逐字保留语义。
- 不得在本目录实现 `config/` 子包已承载的聚焦组件（快照模型、文件监听器、状态模型、
  事件游标族等），本包只组合并消费它们。

# 规范

- 方法族 mixin 之间只通过宿主 `self` 协作；跨 mixin 引用静态方法时按 MRO 凸性规则用
  具体 mixin 类名限定（例如 `ConfigAgentToolsMixin._preflight_custom_tool_factories`），
  每个方法在 `ConfigService.__mro__` 中只允许定义一处。
- 来源权威表 `_SOURCE_LAYER_AUTHORITY` 是每个 `source_key` 分层与 precedence 的唯一登记处，
  任何读路径都只查该表，禁止再自行推导。
- 保持模块级符号的唯一定义点：新增共享符号时落在 `config_service_common.py`，不在各族复制。
- 修改本目录后运行 `uv run ruff check app/services/infrastructure/config_service/`，并跑
  `tests/unit/services/infrastructure/test_config_service.py` 等配置相关用例
  （带进程外 `ulimit`/`timeout` 保护）。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
