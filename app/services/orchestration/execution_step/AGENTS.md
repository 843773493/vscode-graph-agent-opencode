# 目录用途

拥有单次 Agent step 的控制循环、重试策略、失败收口和 Saver/event 端口绑定。

# 可修改内容

- ports 定义显式执行依赖与 Agent factory；runner 拥有单次执行的流程和清理。
- retry 决定继续/停止时机，并仅在最终回答去除引号后精确匹配最后工具文本时还原工具原文；reminders 保留对应控制策略；failures 协调终态。
- model_call 与 stream_bindings 只调用既有 Saver/event 端口。
- approval 从原图 checkpoint 投影待确认调用，经原控制 inbox 提交决定；只持有活动等待句柄，不另建审批存储。

# 不可修改内容

- 不接收 public service 实例或访问其它 owner 的私有字段。
- 不复制 Agent 构建/缓存、provider normalization、canonical payload 或持久化逻辑。
- 不新增 storage/domain/registry 事实或旧 import shim。

# 规范

- public service 用组合调用 runner；retry 必须使用传入端口，不维护第二套依赖来源。
- 用户点名工具的重试要求由 runner 从静态策略启用的 `tools.custom` 中排除持久禁用项后确定；此集合不含 MCP，不等于最终执行工具全集。runtime 只装配依赖，不查询这条重试规则。
- 新状态只属于当前 run；事件契约直接 import event_stream/contracts。
- 所有必需 v2 端口缺失必须明确报错，不允许关闭持久化的降级模式。
- 当前输入与 admission 由原 checkpoint 消费 worker 一次读取并随写入回执交回；runner 直接消费同一份结果。恢复原 checkpoint 只读取输入，取消仍排空该 worker，不按后续查询替换当前输入。
- 失败与取消收尾沿用 runner 原 admission，不在镜像终态前重复查询；消息流仍由原 terminal owner 完整核对 admission 与已提交结果。
