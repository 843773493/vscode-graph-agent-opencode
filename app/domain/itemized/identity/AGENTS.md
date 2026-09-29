# 目录用途

`identity/` 是 v2 稳定身份的领域 owner：`ContextRef`/`ToolSetRef`、受 owner 限定的
`DetailRef`，以及 provider 工具调用身份在 canonical item 上的还原规则。它只做纯值对象
与身份计算，不负责生命周期、I/O 或持久化。

# 可修改内容

- 可以维护 ref 的字段、identity 计算与 manifest token 校验。
- 可以维护 `DetailRef` 的 owner 约束与 `provider_tool_call_id` 的还原规则。

# 不可修改内容

- 不得读取或写入 rollout JSONL/SQLite、detail 文件、Provider 请求或工作区文件。
- 不得导入 rollout_context、编排服务、Agent middleware 或资源平台实现。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- `ref_identity` 必须返回闭合的三元身份；未知 ref 类型必须明确报错，不得静默降级。
- `DetailRef` 只承载逻辑身份，不得混入第二个物理 identity 或路径语义。
- provider 工具调用 ID 只在 `model_call_id` 前缀显式匹配时还原，禁止按正文或 hash 猜测。
