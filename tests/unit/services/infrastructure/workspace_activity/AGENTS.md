# 目录用途

存放 `app/services/infrastructure/workspace_activity/` 活动事件读取、游标和订阅服务的单元测试。

# 可修改内容

- WorkspaceActivityService 的事件分页、重放、心跳和订阅者生命周期测试。
- WorkspaceActivityMixin 的事件持久化、游标窗口及保留边界测试。
- 本目录专属的 pytest fixture。

# 不可修改内容

- 不在此测试跨工作区装配或完整应用 lifespan；这类测试放在 `tests/integration/`。
- 不引入真实外部服务、Provider 或网络依赖。
- 不直接修改 `tests/fixtures/` 中的 fixture 源工作区。

# 规范

- 使用 pytest fixture 注入 service 和临时工作区；每项测试使用独立 `tmp_path`。
- SQLite 连接和异步事件流由创建它们的测试显式关闭，service 由函数级 fixture 统一关闭。
- 正式测试产物写入 `out/tests/unit/services/infrastructure/workspace_activity/test_workspace_activity_service/`。
- 跑测试时使用进程外超时和内存上限。
