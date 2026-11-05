# 目录用途

为 Agent 工具与 Python 脚本传输原 Gateway 工作区发现和联邦搜索合同。

# 可修改内容

- 使用现有 HTTP 连接、认证和 typed 搜索事件的客户端。
- SSE 解码、请求期限、取消关闭和响应协议校验。

# 不可修改内容

- 不实现跨目标队列、领域搜索、第二套分页或缓存。
- 不依赖浏览器状态，不启动或激活工作区，不读取业务存储。

# 规范

- SSE 使用第三方解码器；只在 Gateway 调度，保留原范围、owner、cursor 和错误。
- 每次调用由发起者提供 request_id；断流和协议错误明确失败，不伪报完整无匹配。
- 正常结束但缺最终汇总时，SSE 与 JSON relay 共用验证器抛出 `IncompleteSearchStreamError`，携带 `code=incomplete_stream` 和原 `request_id`，保留既有详细原因；不改标取消、超时、HTTP 拒绝或底层读取异常，不虚构 HTTP 状态。
- 连接由原 GatewaySessionContextClient 持有并关闭，不建立另一套连接生命周期。
