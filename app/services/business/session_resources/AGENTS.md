# 目录用途

聚合 Session 关联的后台任务、Terminal 和 Browser 资源，并将控制与清理分派到真实 owner。

- `service.py`：`SessionResourceService`，负责会话准入、资源列表排序、控制入口与会话清理顺序。
- `registry.py`：`SessionResourceProviderRegistry`，按资源 kind 注册、校验和分派 Provider。
- `providers.py`：三类资源的会话视图与控制适配；后台任务可用动作在其 Provider 中计算。
- `__init__.py`：仅作为包标记，不聚合转导出。

# 可修改内容

- 可以调整会话资源列表、控制分派、清理及 owner 快照到会话视图的适配。
- 可以维护 Provider 注册与资源 kind 一致性校验。

# 不可修改内容

- 不维护资源状态、Thread 关联或 operation lease 的第二事实源。
- 不在此实现 Terminal、Browser 的工作区资源管理、外设操作或底层持久化。
- 不将 DTO 的纯字段映射搬入此目录；它属于 `app/services/mapping/session_resource_mapper.py`。

# 规范

- 调用方从具体模块显式导入；不保留旧根模块、别名或转发入口。
- 列表持有原 Session 读准入，资源状态与操作交给原 owner；清理先删除 Session Jobs，再按注册顺序清理 Provider。
- 保持 Thread 关联、owner revision、历史筛选及错误行为；插件禁用时仅无历史资源的会话可返回空列表。
