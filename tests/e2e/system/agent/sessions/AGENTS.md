# 目录用途

存放 Session 生命周期、派生、目标、资源与失败重试的端到端测试。

# 可修改内容

- Session 公开 API 的真实 HTTP E2E 用例。
- Session 生成、fork、goal、resource 和 replay 行为断言。

# 不可修改内容

- 不在本目录直接固定拼接物理会话路径。
- 不放纯 Agent 工具链或持久化迁移测试。

# 规范

- 会话位置必须通过权威索引或统一路径解析器取得。
- 失败重试必须同时断言原始失败与重试后终态。
- 正式产物写入与本目录测试路径对应的 `out/tests/e2e/system/agent/sessions/`。
