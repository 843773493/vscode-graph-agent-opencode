# 目录用途

本目录拥有消息流事件与状态的确定性归并规则，包括事件投影、块更新和生命周期收敛；输入由 MessageStreamStore 提供。

# 可修改内容

- 可以维护仅依赖传入 state/event 的纯计算函数与必要的领域校验。
- 可以抽取原 Store 的 reducer 和直接计算 helper，保持既有事件投影结果。

# 不可修改内容

- 不得读写文件、SQLite、JSONL、网络、cache 或订阅者。
- 不得获取锁、调度任务或定义第二个持久化、恢复和事务 owner。
- 不得改写已公布状态或缓存对象，也不得引入旧 reducer 转发或兼容实现。

# 规范

- Store 继续独占待提交工作副本、锁、durable append/fsync、cache、fanout、恢复及游标；本目录不保存可变权威状态。
- 归并规则由提交和 replay 共用；Store 原 reducer 实现随调用方迁移物理下线。
- 输入错误按原合同显式暴露，不添加默认值或无关防御分支。
