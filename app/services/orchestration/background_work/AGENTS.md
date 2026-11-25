# 目录用途

编排精确 Session／Thread 的后台工具工作，与 Job 执行和工作区自动化定义分开。

```text
background_work/
├── registry.py             BackgroundTaskRegistry：版本化 handler、启动恢复、执行、取消和外部效果对账
├── mailbox.py              BackgroundMailbox：持久消息收集、消费 checkpoint 与 canonical 提交确认
├── derived_execution.py    参数化派生 handler：独立执行及原 rollout 完成结果冷读
├── compaction.py           冻结范围摘要 handler：完整结果先存 rollout，再确认原任务
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
- `derived_request` 只从已提交 rollout 结果恢复；启动不调用上游，首次访问精确 Thread 后才在旧 holder 失效时接管 fencing 并补确认或终结。`reconcile` 只能有界读取结果，不执行网络请求。
- 派生后台额度固定为一项运行和四个待运行 task；全部派生从原活动 Task 集合核总运行上限2，不按 Thread 合并、不新增分区计数或交互队列。交互满载不落任务；前台提升复用同一 pending task，有总额度才启动且不重置期限。
- 新提交的输入冻结作用域在原派生准入锁内进入，满载不冻结、同键只复用原 descriptor；注册失败由原输入 owner 在同一作用域清理，已有 holder 只观察、不再启动替身 Task。资源停止/删除整批复用该调度门，不在取消前项时启动本批 pending。
- 派生 handler 提供当前 attempt 的固定绝对 deadline，排队、运行与发布共用该期限；首次值来自冻结 descriptor，压缩首次上游失败终结后的唯一补 attempt 在原任务 metadata 登记新前台期限，不改 descriptor／冻结输入。查询、出队和在途提升不重置期限，不新增期限定时器。取消/停止/删除的排空等待在 Session gate 外执行，让原发布与取消结算能取得同一短 gate。
- 派生 handler 在 `derived_publication` 短准入内提交完整结果，然后返回仅含结果引用的完成 step；不能写普通 mailbox。网络等待必须在发布 gate 外，ready 不持运行 lease 或驻留 blocker。
- 显式前台补尝试先等原 holder 退出，沿原任务 CAS 和唯一 retry lease 再准入；终态 lease 不复活。压缩 owner 从原 ModelCall／terminal failure 核首次上游失败与剩余32次预算，新前台操作期限在同一 retry CAS 固定，已完成块只读复用；冷恢复仍禁止续跑半链。普通重复准备不恢复耗尽预算，显式手动入口才可在失败后发起新评估。
