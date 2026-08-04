# tests/unit/services/business/job

## 目录用途

存放 `app/services/business/job/` 任务状态、控制和生命周期服务的单元测试。

## 可修改内容

- JobService 及相关状态对象的单元测试。
- 测试专用事件总线和执行器 fake。

## 不可修改内容

- 不调用真实 Agent 或模型 Provider。
- 不在此启动完整后端任务流程。

## 规范

- 任务状态转换必须断言权威返回对象和事件结果。
- 异步任务必须在测试结束前显式完成或取消。
