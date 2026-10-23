# 目录用途

存放 Communication 编排层的单元测试，覆盖冷观察读取、Inbox 接纳和等待恢复。

## 按测试职责查找

```text
communication/
├── test_communication_observations.py  冻结分页与游标、wait deadline、通知和冷恢复
└── test_inbox_admission_worker.py      Inbox 启动恢复、claim 冲突、wakeup 和 binder 重试
```

两组测试都通过临时 `SessionControlStore` 验证编排服务与持久状态的交界；前者验证已提交的观察和等待事实如何读取或恢复，后者验证 Inbox 如何被接纳并绑定到执行。会话观察和任务接纳是不同业务流程，新增用例应落到对应的测试文件。

# 可修改内容

- Communication 编排服务、等待与观察恢复的单元测试。
- Inbox admission worker 的状态恢复、claim、wakeup 和 binder 测试装具。

# 不可修改内容

- 不访问真实工作区或远端 Gateway，不启动真实 Job。
- 不在这里重新实现 SessionControlStore、会话生命周期门或 communication business contract。

# 规范

- 所有可变会话控制数据放在测试提供的临时目录中；测试不得复用项目根目录或只读 fixture 作为运行工作区。
- 冲突、deadline、恢复和通知断言必须检查持久状态及后续消费者可见结果，不能只验证 helper 返回值。
- binder 失败必须验证 Inbox 仍可恢复；观察游标恢复必须验证原分页快照和原 deadline。
