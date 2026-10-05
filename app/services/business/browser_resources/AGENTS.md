# 目录用途

Browser 领域 owner，负责创建及真实工具调用；共用工作区资源的保留、关联、lease 和释放规则。

# 可修改内容

Browser owner、operation 与对应行为验证。

# 不可修改内容

不得读取 Node 状态文件或从历史工具输出反推领域归属；不得在这里实现 Playwright、HTTP 或 Gateway 路由。

# 规范

来源 Thread 不可变，所有操作经唯一 lease 账本；外部结果未知须持久暴露，禁止盲目重放。
