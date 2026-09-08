# 目录用途

拥有单次 Agent step 的控制循环、重试策略、失败收口和 Saver/event 端口绑定。

# 可修改内容

- ports 定义显式执行依赖与 Agent factory；runner 拥有单次执行的流程和清理。
- retry 决定继续/停止时机；reminders 保留对应控制策略；failures 协调终态。
- model_call 与 stream_bindings 只调用既有 Saver/event 端口。

# 不可修改内容

- 不接收 public service 实例或访问其它 owner 的私有字段。
- 不复制 Agent 构建/缓存、provider normalization、canonical payload 或持久化逻辑。
- 不新增 storage/domain/registry 事实或旧 import shim。

# 规范

- public service 用组合调用 runner；retry 必须使用传入端口，不维护第二套依赖来源。
- 新状态只属于当前 run；事件契约直接 import event_stream/contracts。
- 所有必需 v2 端口缺失必须明确报错，不允许关闭持久化的降级模式。
