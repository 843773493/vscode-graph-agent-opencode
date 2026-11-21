# 目录用途

编排精确 Session／Thread 的后台工具工作，与 Job 执行和工作区自动化定义分开。

```text
background_work/
├── registry.py             BackgroundTaskRegistry：版本化 handler、启动恢复、执行、取消和外部效果对账
├── mailbox.py              BackgroundMailbox：持久消息收集、消费 checkpoint 与 canonical 提交确认
└── system_time_emitter.py  系统时间消息 handler，消息和任务 checkpoint 同事务提交
```

## 可修改内容

- 后台任务运行、恢复、取消、generation 检查和 holder 生命周期。
- 邮箱等待、消费重放、提交确认及恢复对账。
- 已注册 handler 的版本化执行逻辑。

## 不可修改内容

- 不实现工作区自动化任务定义、触发调度或 Job／Agent step 执行；这些分别归 persistent_tasks/、JobService 和 execution_step/。
- 不另存任务或消息；持久事实由 BackgroundWorkStore 定位原 SessionControlStore，纯数据合同归 app/domain/background_work.py。
- 不在外部等待期间持有写事务或 Session gate，不盲重放未知外部效果。

## 规范

- 注册器只保留本地活动 Task 和候选 Session ID，不缓存历史任务正文；reconcile_required 即使没有本地 Task 也仍是活动任务。
- 邮箱不是事件广播总线；进程内 Event 只唤醒，消息与消费回执始终从控制库读取，canonical 提交后才确认消费。
- 收集按小页推进持久消息范围；SQLite 操作在受保护 worker 中结算后才释放准入，不在事件循环复制、重写累计正文。消息正文仅在终态返回或重放时组装一次，重启沿原 collected_at 恢复消费期限。
- 扩展 factory 的 CustomToolFactoryContext.background_message_bus 是既有公开字段；内部使用 BackgroundMailbox，在该边界一次传入，不增加第二字段或兼容实现。
- handler 的 kind/version、持久身份、消息顺序、期限及错误必须沿原合同保持。
