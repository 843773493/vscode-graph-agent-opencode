# 目录用途

验证 Session fork 的远端回执、发布和恢复服务边界。

# 可修改内容

- fork 跨 owner 集成测试、故障注入及原操作恢复断言。

# 不可修改内容

- 不在测试中实现 fork 业务状态机或写入本应由产品入口产生的发布状态。
- 不复制模型协议、存储和 fork 装具实现。

# 规范

- 通过 pytest fixture 复用已有装配，使用同名正式测试工作区与产物目录。
- 同时验证错误归属、持久副作用和原操作恢复；传输替身不作为真实网络验收。
- 保留正式 matrix 的进程外保护与原 fork 期限，不覆盖 deadline 实现。
- `test_thread_rewind.py` 经真实 Gateway 消息、fork 与 main regenerate 生成历史，对照独立 Thread 的完整 SQLite/JSONL、重启历史和后继请求；复用 model_stream 与既有只读封存请求审计，不手填 checkpoint、CSM 或视图。
