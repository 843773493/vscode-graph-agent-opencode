# 目录用途

编排 MCP 连接操作的精确 thread 后台观察、原子邮箱通知与内部执行投递。

# 可修改内容

- typed/versioned 等待 descriptor、checkpoint 及原 BackgroundTask handler。
- 连接终态与 RuntimeSourceDispatcher 之间可恢复的单一投递链路。

# 不可修改内容

- 不实现 OAuth、连接、token 交换、远端业务重放或第二套任务／队列存储。
- 不改变原 Turn、目录冻结或后台邮箱消费确认合同。

# 规范

- 观察与投递分步；checkpoint、消息与待投递引用由既有控制库原子提交。
- 恢复只读取原精确 owner 的事实；新准入检查当前权限、原期限及删除 fence。
