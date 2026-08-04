# tests/unit/services/infrastructure/turn_history

## 目录用途

存放 `app/services/infrastructure/turn_history/` 日志、索引、恢复和压缩基础设施的单元测试。

## 可修改内容

- TurnHistoryStore、索引、时间线和故障恢复测试。
- 文件系统故障注入与临时历史 fixture。

## 不可修改内容

- 不在此测试业务层历史展示或投影规则。
- 不写入真实工作区会话目录。

## 规范

- 恢复测试必须断言权威文件和索引的一致性。
- 所有产物必须使用 pytest 临时目录。
