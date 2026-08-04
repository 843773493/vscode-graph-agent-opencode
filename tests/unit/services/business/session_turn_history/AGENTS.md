# tests/unit/services/business/session_turn_history

## 目录用途

存放 `app/services/business/session_turn_history/` 历史迁移、投影、展示和业务查询的单元测试。

## 可修改内容

- Turn 历史服务、迁移器、投影器和 mutation 行为测试。
- 测试专用事件与历史存储 fixture。

## 不可修改内容

- 不把基础设施存储恢复测试混入本目录。
- 不启动真实 SSE 或完整后端进程。

## 规范

- 业务投影必须以稳定事件身份和顺序为断言依据。
- 测试数据必须隔离在 pytest 临时目录。
