# 目录用途

Browser 领域 owner，负责创建及真实工具调用；共用工作区资源的保留、关联、lease 和释放规则。

## 文件职责树

```text
browser_resources/
├── owner.py       Browser 领域状态与生命周期，委托共享资源 owner 处理通用保留和释放
└── operations.py  单次受信 Browser 调用，持有 operation lease 并核实外部回执
```

调用路径从 `app/agents/tools/browser.py` 进入 `BrowserResourceOwner.operation()`，再由 `BrowserOperations` 执行单次调用；需要新增跨 Browser/Terminal 共用的 lease 规则时，归 `workspace_resources/`，只影响 Browser 的 Playwright 事实和动作时留在本目录。

# 可修改内容

Browser owner、operation 与对应行为验证。

# 不可修改内容

不得读取 Node 状态文件或从历史工具输出反推领域归属；不得在这里实现 Playwright、HTTP 或 Gateway 路由。

# 规范

来源 Thread 不可变，所有操作经唯一 lease 账本；外部结果未知须持久暴露，禁止盲目重放。

工具与管理操作的成功、正式失败回执共用身份、当前整数版本、核实状态及持久化链。校验、投影或提交失败须将本次精确lease保留为待核实，失败回执的处理错误不能绕过unknown；保留原失败cause。正式失败报告未知页面转换时仍返回原HTTP错误并保护占用；完整已核实失败保留原异常，正常释放本次lease，不将动作失败冒充资源已停止。
