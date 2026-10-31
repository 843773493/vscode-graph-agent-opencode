# 目录用途

`fork/` 负责精确 Thread 的冻结快照、view 前缀、历史 scope binding 与保留策略。

# 可修改内容

- 可以实现捕获、原字节安装、typed 正文依赖闭包和既有 owner 的目标引用保活。

# 不可修改内容

- 不得读取 v1 runtime、逐项重分配历史 ID、重算历史业务 hash 或回源读取正文。
- 不得恢复源 Job、锁、连接、准入或 pending 工具执行。

# 规范

- 当前存储归属由目标 database_meta 决定；历史执行 scope 通过目标本地冻结 binding 授权。
- 保留历史 ID、时间、事实状态与封存 bytes；新输入只创建当前 owner 的新执行。
- 可见前缀与正文依赖闭包分开；A→B→C 不在线回读 A/B。
- 静态结构与迁移SQL唯一定义在 `sql/`；Python显式加载并保留事务、条件、版本与参数协调。

- `path_scope.py` 持有每个 storage 实例的私有 ContextVar，并唯一解析受其约束的 Thread 身份和 Session/Thread 节点；普通节点交 catalog resolver，冻结来源只读，目标 capability 仅授予冻结 creation record 的节点。
- 冻结文件传输的原生读取、写入、同步、关闭、完整核验与失败清理统一经原 shielded worker 结算；接收句柄在 worker 内先登记到本次调用，打开后取消但未交付也须关闭。原导出/创建锁持有至结算，先排空分块写入者再删除临时快照，保留原错误链、字节和 marker 合同，不回读 live 来源。
