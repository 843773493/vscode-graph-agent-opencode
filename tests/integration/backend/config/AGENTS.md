# 目录用途

验证 Workspace 配置应用、SQLite 状态与 HTTP 投影之间的合同。

# 可修改内容

- 配置候选失败、恢复及重试的跨模块测试和 fixture。

# 不可修改内容

- 不手工写入业务状态替代配置服务的候选接纳、claim 或 promotion。
- 不把进程内 ASGI 验证描述为独立 Backend/Gateway 或真实启动健康证明。

# 规范

- 工作区及产物按测试文件隔离到 `out/tests/integration/backend/config/`。
- 原异常身份、持久事实、API 序列化和显式重试须一起核验。
