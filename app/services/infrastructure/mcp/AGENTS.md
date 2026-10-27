# 目录用途

`app/services/infrastructure/mcp/` 负责读取 MCP Server 配置、建立 MCP Client 连接，维护唯一的工具目录 owner `McpCatalogOwner`（完整 relist、不可变 catalog revision、`tools/list_changed` 通知消费、连接 generation lease、`mcp.catalog/*` typed 轻量事件），并将目录中的工具适配为 Agent 可使用的 LangChain 工具。

# 可修改内容

- MCP stdio 与 Streamable HTTP Client 配置解析。
- `oauth.py` 通过官方 OAuthClientProvider 完成 discovery、PKCE、state、交换与刷新；只将已确认的授权需求交给 Owner。OAuth 专属 SDK 日志及异常不可携带 token、state 或响应正文；未知401与混合清理异常仍明确失败。
- `oauth_credentials.py` 实现官方 TokenStorage，凭据按工作区／Server／endpoint／回调隔离，使用既有跨平台私有权限和原子文件原语；冷启动恢复原绝对过期时间与 discovery endpoint，不写入会话正文或普通状态表。
- MCP Server 连接生命周期、工具目录 relist/通知消费/generation lease 与调用、状态查询。
- `connection_service.py` 协调持久连接意图、短观察、deadline、取消与私有 OAuth 回调；实际网络及目录仍由原 Owner 发布。`operation_store.py` 只使用唯一 WorkspaceStateStore，持久幂等 alias 与安全状态，不保存授权 URL、code、state 或 token。运行时断开不改 JSONC，重启仍按已启用配置创建新连接；遗留操作只对账，不重放交换。
- `session_runtime.py` 在每个 Server 的原 task 中创建、初始化并关闭 SDK session；现有 LifetimeScope 持有关闭与排空回调，外层启动／关闭 task 不拥有 SDK CancelScope。目录发布和通知 fencing 仍只由 `McpCatalogOwner` 决定。
- stdio 与 Streamable HTTP 统一使用 SDK transport 和 `CancellableMcpClientSession`；`invocation.py` 仅传递当前调用进度与取消投递事实，不充当持久 invocation lease。取消通知按原 RPC ID 投递，投递成功不代表远端停止。
- `mcp.catalog/*` 目录事件合同的发布接线。
- MCP Tool 名称映射和 LangChain 适配。
- `tool_adapter.py` 是 SDK 完整分页和调用的唯一工具适配入口；公开参数不能与 Python 注入参数重名后被吞掉，协议结果继续复用现有库转换，不另写传输协议。
- 扩展目录 binding（ExtensionCatalogBindingRef）与目录 generation lease 的维护。
- 扩展工具的有界分组导航和显式 `model_visible` 完整定义指引，以及经 `McpCatalogOwner` 的唯一发布回调，把同 revision 的指引父行、typed members 与完整定义 facets 原子发布到既有 ResourceRegistry；父行正文不混入完整目录或 provenance。
- `tool_definitions.py` 只从原 activation 恢复完整定义；`tool_search.py` 负责确定性匹配、逐项结果与有界分页，不访问当前网络目录或执行候选。定义正文和来源沿现有资源 body store/Saver 保存，不增设 schema 库。
- 扩展 dispatch binding（ExtensionDispatchBindingRef）从已封存的目录 payload/members 派生；targets 是 members 的只读视图，不保存第二份权威映射。
- MCP 可执行 typed member 的 revision 保存 Owner 实例与连接代际组成的精确 connection_ref；已验证定义在断开／SDK 再授权时由同一 publication 保留，以 `mcp-catalog-only:v1:disconnected|authorization_required` 表达目录专用成员，不进入执行 targets。定义正文与 readiness 同批冻结，目录语义 revision 独立保存，搜索 cursor 同时约束 readiness；不建立目录缓存或借其它会话重建当前事实。Agent 运行时只经 Owner 解析执行目标，不捕获初始 MCP 工具，也不按同名同 schema 迁投新连接。
- `execution_handles.py` 属于原目录 Owner，只保留精确连接的短期对象引用；模型发送前经冻结 activation 登记有界资格，响应后保留实际输出的信封调用，既有 HITL edit 仍可选择原封存目录内的目标。期限由同一 MCP 配置族的 `supervision.sealed_call_timeout_seconds` 控制，不在重试中续期。
- MCP 执行目标沿 `execution_handles.execution_target` 核对原资格并占用唯一 ExternalResourceLeaseLedger；typed resource-use 来自受信调用上下文。SDK 确认结果后才释放，未知操作保留 reconcile_required，资格到期／连接关闭与远端副作用分别核实，禁止自动重放同一 operation。
- 配置候选只在原 Owner 内准备，使用其每 Server 子 scope；配置 shadow 只回收未提交候选，不能在后续无关配置更新时关闭 active 连接。HTTP 先建后换；旧 stdio 停止前经原配置 recorder 写 journal，并同步撤销准入，等待原 task 关闭后再启动候选。无法保留旧运行态的失败沿原配置 owner 报 recovery_required。
- `retirement.py` 是原 Owner 的退役排空组件，只保留 runtime 引用并读取原账本；正常替换保留已有资格，禁用／移除同时撤销更早代际的新准入。排空和关闭各自使用原封存资格秒数作为上限；关闭 socket 不结清结果未知操作。

# 不可修改内容

- 不实现 Agent 编排、API 路由或前端展示。
- 不在代码中硬编码 MCP Server 命令、URL、凭据或工作区路径。
- 不把 MCP Server 运行状态写入 Gateway 全局目录。
- 目录事件只携带 identity/revision，不得携带工具 schema 正文/credential，不得进入 job.events；事件消费者不得自行注册 ResourceRegistry source 或 CSM 来源。完整目录 payload 只能经唯一 owner 的发布回调进入 Registry。
- 不建立独立的 MCP activation writer/catalog；execution/model-call snapshot、受保护正文与 assembly binding 的提交/恢复归既有 ResourceActivationCoordinator 和 Saver/ContextStore。
- 不得绕过 `McpCatalogOwner` 建立第二套工具发现/刷新路径。
- sealed binding ref 一经返回不可变；目录增删改/权限变化不得改写已封存 ref，旧 tool call 只按 sealed ref 解析。
- `extension_dispatch_binding_hash` 只服务扩展 dispatch/restore 校验，不得混入 `context-plan-hash:v2`、Provider `ToolSetRef` manifest 或 `request_hash`；内层目录/指引变化不得制造 `toolset_changed` epoch。

# 规范

- MCP Server 配置错误、连接失败和协议错误必须直接抛出详细异常。
- stdio Server 必须使用命令与参数数组启动，不得通过 shell 字符串执行。
- MCP 工具名称必须带 Server 命名空间，禁止同名工具静默覆盖。
- 目录 relist 必须完整读取并验证；连续 relist payload 不变时不得推进 revision；增删改发布新 immutable revision，删除以 tombstone 事件标记；目录为空也发布固定 envelope/revision。
- 目录 generation 只在 revision 实际推进时递增；binding ref 的 binding_id/binding_hash 必须可重算核对。
- 完整公开 schema（含 required、additionalProperties、组合与本地引用）经统一导出后用于目录、guidance、binding 和信封校验；properties 摘要只作展示。v3 binding 和 lineage 同时封存完整 schema 与连接身份，不可由 v1/v2 历史指纹推导执行资格；历史读取保留原件，旧契约恢复执行必须明确拒绝。
- `binding_hash` 与 `extension_dispatch_binding_hash` 必须复用 domain 的 `sha256_jcs` JCS token（`sha256:jcs:v1:<64hex>`），不得自造第二套 canonical 编码或摘要前缀。
- 指引只从已验证目录派生并保持有界确定性；外部 description 是不可信数据；binding 与指引必须同 revision 原子冻结，relist/派生失败 fail closed，不半发布。
- 导航、自动完整定义与搜索使用同一 ToolMetadata 和完整有效策略；软隐藏可搜索，硬隐藏与执行禁用不投递定义或导航。工具选择提交后在原 Owner 发布锁内重发指引与 members，不联网刷新；订阅随 Agent generation 的 LifetimeScope 释放。
- 安全 model-call preparation 只读取 Registry 已发布的 snapshot/revision，不在请求期 relist、读取文件或访问 MCP 网络；无通知 server 的显式刷新仍由目录 owner 在发布边界执行。
- 历史 binding/快照丢失必须显式 `extension-catalog-unavailable` 并拒绝 dispatch，不从当前 MCP 目录、当前同名 target 或空目录恢复。
- 不具备 `tools/list_changed` 通知能力的 server 由 owner 在显式目录刷新边界 relist并发布，不在 model-call preparation 临时刷新，不得谎称立即生效。
- 凭据只允许通过环境变量引用解析，不得写入日志或模型工具描述。
- Activity 只发布受信调用身份及数值进度，不转发 Server 自由进度正文。取消、超时和断线无法确认副作用时，使用同一 typed 结果未知回执，保留调用配对、artifact 与不可自动重试事实。
