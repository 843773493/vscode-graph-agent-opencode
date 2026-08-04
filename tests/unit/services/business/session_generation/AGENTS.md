# tests/unit/services/business/session_generation

## 目录用途

存放 `app/services/business/session_generation/` 会话生成规则和 Provider 编排的单元测试。

## 可修改内容

- 生成服务、消息派发、报告和 Provider 选择测试。
- 测试专用生成器与服务 fake。

## 不可修改内容

- 不调用真实模型或启动完整 Agent。
- 不在此测试 Gateway 调度器。

## 规范

- 外部调用必须显式替换并验证参数。
- 生成状态转换必须断言权威服务结果。
