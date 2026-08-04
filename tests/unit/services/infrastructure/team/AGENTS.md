# tests/unit/services/infrastructure/team

## 目录用途

存放 `app/services/infrastructure/team/` 团队状态持久化的单元测试。

## 可修改内容

- TeamStore 的读写、并发和恢复行为测试。
- 临时团队状态 fixture。

## 不可修改内容

- 不测试业务协调规则。
- 不写入真实会话目录。

## 规范

- 所有存储必须位于 pytest 临时目录。
- 持久化错误不得被静默忽略。
