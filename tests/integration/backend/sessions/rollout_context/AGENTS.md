# 目录用途

验证 Saver、canonical domain、SQLite/JSONL 和上下文 owner 之间的跨模块合同。

# 可修改内容

- item/commit/recovery、checkpoint、Turn、selection、fork 和迁移集成测试。
- 跨模块测试 fixture 与损坏注入断言。

# 不可修改内容

- 不得恢复旧 app.core rollout shim 或伪造被测持久化结果。
- 不得把本地协议桩或回放结果当作真实 Provider E2E。

# 规范

- 使用 pytest fixture 注入；真实 SQLite/JSONL 必须放在同名正式测试输出中。
- 失败路径必须同时断言明确错误和已提交数据未被错误改写。
