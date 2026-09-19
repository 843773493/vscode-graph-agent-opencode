# tests/unit/services/orchestration/communication

## 目录用途

存放 InboxAdmissionWorker 的单元测试：状态索引恢复、claim 幂等/冲突分类、失败可恢复重试。

## 可修改内容

- worker 生命周期与单轮消费测试。
- fake binder 与临时 session-control store fixture。

## 不可修改内容

- 不访问真实会话、不扫目录、不运行真实 Job。
- 不在此测试 facade/gate 语义（属 business/communication 测试）。

## 规范

- claim 冲突断言必须校验 outcome 闭集分类，不只断言异常类型。
- binder 失败必须断言 last_error 落库且 state 保持 target_accepted。
