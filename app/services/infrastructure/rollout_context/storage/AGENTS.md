# 目录用途

`storage/` 负责 v2 JSONL、catalog、commit 和恢复边界的基础持久化适配。

# 可修改内容

- 可以实现 JSONL 编解码、SQLite 索引和崩溃恢复的基础组件。
- 可以调用 `app/domain/itemized/` 的值对象与 JCS hash。

# 不可修改内容

- 不得实现 LangChain、Provider wire、业务规则或一次性 v1 import reader。
- 不得建立第二份 canonical payload 事实源。

# 规范

- JSONL item 必须不可变、使用 RFC 8785 JCS；offset/length 必须与唯一权威 commit 状态一致。
- 失败必须明确报错，不得静默跳过损坏记录。
- 静态结构与迁移SQL唯一定义在 `sql/`；Python显式加载并保留事务、条件、版本与参数协调。

- 附件引用与系统生成块的消息投影归 checkpoint/projection；基础 storage 不持有附件 projector 或提供转发方法。

- Fork 来源冻结、未发布目标 capability 作用域及其 Session/Thread 节点解析由 `fork/path_scope.py` 独占；storage 只消费该 owner 的 `session_node` / `thread_node`，不保留私有路径分支或转发入口。

- 备份/离线恢复的无状态校验只归 `rollout_offline_restore.py`；目录及已存在文件的 durability 屏障复用 `app/core/atomic_fs.py`，不得恢复 filesystem mixin 或基础 facade 的私有转发。
