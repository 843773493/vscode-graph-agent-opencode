# tests/unit/services/business/team

## 目录用途

存放 `app/services/business/team/` 团队协作、消息和规则服务的单元测试。

## 可修改内容

- 团队协调服务、看板规则和消息语义测试。
- 团队仓储 fake 与 fixture。

## 不可修改内容

- 不访问真实会话或运行真实子 Agent。
- 不在此测试基础设施 TeamStore。

## 规范

- 协作状态变更必须断言持久化调用和返回对象。
- 并发路径应使用确定性的同步原语。
