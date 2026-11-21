# 目录用途

`app/services/infrastructure/events/` 承载 OpenSpec 3.8 的通用 `EventChannelService`：按 `kind/{参数}` 命名的进程内通知 channel，为每条 channel 提供订阅者隔离的有界队列、单调递增 event sequence、可选短期历史与 cursor 重放，以及可插拔的 `gap` / `fail_closed` 溢出策略。现有 `session.trace`（已提交 Trace 事件）、`job.events`（Job event bus）与 `resource.observe`（资源观察通道）都是它的 typed adapter；`resource.state/*`、`config.lifecycle/*`、`context.source/*`、`mcp.catalog/*`、`task.lifecycle/*` 的 typed 轻量事件值对象合同也在本目录定义。

`session.trace` 按 Session 或 Thread 交付已落盘事件，仅保留每个订阅者至多 64 条待消费记录；无订阅者不创建通道。溢出立即失败并断流，客户端使用最后收到的 Trace 游标从 SQLite 补读。正文合同仍归 Trace owner，本目录不定义第二份正文协议。

`user.questions/{session_id}:{thread_id}` 只通知留言失效；SSE 初始／重连读取当前数量及 revision，正文沿原有界分页读取，无订阅者不建通道、最后订阅断开后回收。

`workspace.resources/{kind}` 由工作区资源 owner 持有，只通知清单失效，不携带正文、输出或第二份资源事实；每个订阅者最多64条待处理通知，消费时合并资源ID，溢出后整范围失效；首次／重连通过原 owner 补读。

`job.events` 仅承载实时订阅，不保留历史。Job owner 在最后一个订阅解除或溢出后显式回收通道；无人订阅时发布只执行持久化监听器。其它 channel 的历史与生命周期由各自 owner 决定，不自动回收。

# 可修改内容

- 可以维护 `EventChannelService`、`EventChannel`、订阅句柄与投递/回执值对象。
- 可以在 `channel_events.py` 中扩展各 channel 的轻量事件合同（字段白名单、值校验、channel 名构造）。
- 可以补充针对本目录的单元测试与诊断字段。

# 不可修改内容

- 不得把本服务做成 durable 事实库：短期历史只用于 cursor 重放辅助，不替代 Session 事件提交，业务 durable state 仍由各 domain owner 保存。
- `channel_events.py` 的轻量通知值对象不得携带正文、diff、credential 或宿主机路径；新增字段必须先过字段白名单并满足轻量红线。
- 不得让资源事件进入 job 队列，也不得在此建立第二个通用 EventBus 语义之上的并行传输。
- 不得引入可安装的 provider/loader/reaction 或运行时可配置的 channel kind 扩展点；新增 kind 属于代码变更。
- 不得在本目录读取文件、访问网络或承载业务决策（激活、注入、epoch 等一律不属于事件传输）。

# 规范

- channel 名必须经 `channel_name` / `parse_channel_name` 构造与校验；kind 是闭集，新增 kind 需同步更新 `CHANNEL_KINDS` 与本文件。
- channel 之间故障域隔离：一条 channel 的订阅者溢出、队列积压不得影响其它 channel。
- `gap` 策略必须保持「丢最旧一条 + gap 标记 + 消费后恢复投递」语义；`fail_closed` 策略必须「队列满即记录溢出错误并从后续投递移除」；外部 sink 订阅只支持 fail_closed。
- 发布是同步纯内存操作：不做 I/O、不等待 consumer；发布方不得把投递回执当作持久化确认。
- 失败必须显式抛出（spec 冲突、非法名字、非法参数、溢出错误），不得返回虚假默认值。
- 代码注释使用中文；修改本目录后需运行 `uv run ruff check` 与对应单测。
