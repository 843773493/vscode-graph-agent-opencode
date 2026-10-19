# 目录用途

存放真实 Agent 上下文、Provider 派发及持久化 provenance 的系统 E2E。

# 可修改内容

- 真实 Gateway、Workspace、外部 Provider 与冷恢复组成的上下文审计场景。

# 不可修改内容

- 不使用模型替身、录制回放或测试专用业务写入替代真实请求入口。
- 不修改生产源码或其它测试分区的共享 fixture。

# 规范

- 同一次调用按 Session、Thread、Execution、ModelCall 和 assembly 身份逐项对账。
- 正式工作区及产物镜像本目录测试路径，外部依赖失败必须保留原始证据。
