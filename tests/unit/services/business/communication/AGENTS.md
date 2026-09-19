# tests/unit/services/business/communication

## 目录用途

存放 `app/services/business/communication/` 跨 Session 通信 typed admission/wait 合同层的单元测试。

## 可修改内容

- canonical 地址/main binding、admission 幂等与 reply 因果合同测试。
- wait selector、DurableDeadline 恢复预算、状态聚合与 deadline 竞态裁决测试。

## 不可修改内容

- 不访问真实会话、SQLite 或运行真实子 Agent。
- 不在此测试 Gateway 路由或 UI 行为。

## 规范

- 所有时间边界必须使用 fake WaitClock 推进，不缩短产品 1-300 秒合同。
- 幂等冲突断言必须校验闭集错误码，而不是只断言异常类型。
