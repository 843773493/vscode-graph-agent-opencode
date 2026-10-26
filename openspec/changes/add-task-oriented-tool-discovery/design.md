## Context

动机见 proposal.md。本设计以当前已落地的唯一 owner／Registry／activation／seal 链路为基线，并承接进行中的上下文与 itemized 规格，不另建运行时。

源码事实：

- `mcp/catalog_owner.py` 已完整 relist、消费变化通知并发布不可变目录；现有 connection generation 主要约束迟到通知与 relist，尚不是执行调用的完整生命周期。
- `mcp/tool_guidance.py` 只输出截断描述和参数字段摘要；`catalog_activation.py` 将其作为一个已选正文父行发布，typed members 与 lineage 单独保存。
- 完整 schema 切片已完成：`model_tool_schema.py`、目录 v2 身份和信封校验共用完整公开契约，MCP SDK 薄适配保留合法附加参数；旧属性指纹和参数裁剪已移除，任务 2 的完成状态保持不变。
- 完整定义的冻结正文不能由 schema hash 代替；后续搜索切片应沿现有 Registry 的 definition facet、activation 和 Saver 接入，避免从 live 工具对象重建历史定义。
- `agent_factory.py` 捕获 initial targets，再按 schema hash 查当前 publication，不能用于连接热替换后保证原执行身份。
- `/mcp` 当前为 `restart_workspace`；`workspace_lifecycle.py` 明确缺少由原 task 排空旧 generation 的 supervisor。
- 已有 ToolPolicyResolver 的 `model_visibility_locked`、目标级 HITL、ToolOutputStore、BackgroundTask handler／资源展示、精确 thread 邮箱、RuntimeSourceDispatcher 和含 `mcp_connection` 的 ExternalResourceLeaseLedger；应复用这些机制。邮箱 emit 只持久投递并唤醒收集器，不会自动调度 Agent；首次未授权的启动准入、通用 MCP OAuth 和恢复投递接线需要补齐。

参考：[Composio Connect](https://docs.composio.dev/docs/composio-connect#available-mcp-tools) 的任务搜索、完整 schema、连接管理、批量执行和授权等待职责；本地产品不依赖其托管目录或云端控制面。附件中的 `additional_tools` 仅作为 Provider 特定优化的参考，不成为通用协议前提。

## Goals / Non-Goals

**Goals:**

- 只新增一个 Provider 发现入口，连接与执行复用固定信封，长等待复用持久后台机制。
- 全链路使用完整契约，保证定义、版本、连接代际、权限、真实结果可核对。
- 按工作区监督连接、受控热替换、排空旧调用，提供透明的失败和重启对账。
- 以纵向切片迁移直接／间接消费者和测试，每个切片完成后物理删除对应旧路径。

**Non-Goals:**

- 不实现应用商城、任意联网目录抓取、向量数据库、云端凭据托管或独立 MCP 进程。
- 不新增 multi-execute、get-schemas、wait-for-connections 或远端 Python/Bash 工具；现有本地工具继续承担对应职责。
- 不改内置工具的直接注册方式，不引入 Provider 私有增量声明，不承诺缓存命中。
- 不把 MCP prompts/resources/server instructions 自动作为高优先级指令；后续资源接入仍走既有资源平台。

## Decisions

### 1. 通用搜索是唯一新 Provider 入口

`search_tools` 接受两种互斥的只读请求：`query` 按任务搜索，或非空 `tool_ids` 按 ID 获取定义；公共参数为 `limit`、`cursor`、`max_response_bytes`。缺少选择器、同时给出两种选择器、非法范围或失效 cursor 均明确报错。默认一次返回少量候选；精确模式按请求顺序返回逐 ID 结果，不把缺失项伪装为空 schema。

结果统一承载 `tool_id`、origin/group、用途、完整定义的内联值或可读取引用、schema hash、catalog revision、匹配理由、execution/model-visible 状态、readiness、缺失连接和后续控制目标。匹配理由来自确定性匹配事实，不声称已执行任务或保证工具效果。首期仅聚合已有 MCP、本地扩展及本地连接控制目标；内置直接工具仍由现有 ToolSet 管理，日后纳入搜索也须声明实际调用入口。

搜索使用调用所属的冻结目录快照；不在模型请求 preparation 或搜索期间临时访问任意 MCP Server。新发布目录按既有激活边界进入下一可见快照。排序以规范化名称／关键词／描述匹配分数和稳定 ID 决胜；首期不额外调用 LLM 或 embedding 服务。分页绑定目录、策略 revision 和查询身份，变化后返回 snapshot conflict／明确重新搜索指引。

每个 schema 必须完整、自包含，保留 `$defs` 与本地引用；首期不能本地解析的外部引用明确拒绝，不静默访问网络或降为宽松校验。容量不足时少返回候选并给 cursor；单个定义无法内联时，经现有 ToolOutputStore／会话 artifact 保存完整定义或包含它的完整工具结果，返回有界 `schema_ref`、`read_path`、`content_sha256`、体积、schema hash 与冻结 provenance，并标记 `schema_state=needs_read`。只有完整内联定义才标记 `inline_complete`，读取引用前不能将预览当作完整契约。引用也无法放入预算、物化失败或内容不可恢复时明确报错，不退回截断 schema。

响应预算必须同时遵守既有工具策略与 ToolOutputMiddleware 的字节／行数限制，不单凭 search 的 max_response_bytes 判断成功。测试应穿过真实输出中间件，证明模型最终收到完整内联定义或明确的可读取引用；物化副本只是派生工具结果，定义权威仍为冻结资源，不新增 schema 数据库、fetch 工具或 artifact writer。精确请求受同一策略和体积约束。

选择新增入口的原因是任务级能力检索与文件 `grep`／历史 `search_context` 的合同不同。底层复用现有资源身份、目录、结果存储和分页习惯，不为减少名称而修改这些工具的业务语义。

### 2. 完整公开 schema 是唯一输入契约

复用并收敛 `export_model_tool_json_schema`；完整 schema 的规范值进入目录语义 revision、target schema hash、发现／自动注入正文和冻结 binding。仅改变顶层 required、additionalProperties、组合规则或 `$defs` 必须反映到身份中。参数摘要只是派生展示数据。

采用既有 canonical JSON／JCS 摘要设施，不再为同一契约维护可分歧的属性指纹或 `default=str`／repr 容错。binding/hash 合同变更明确版本化；历史 schema／handle 不可恢复时拒绝执行并保留原件，不用当前同名工具补洞。回放可读与继续执行是不同能力，不能为了恢复执行而重写历史。

完整定义以同一 ResourceRegistry 中每目标的 definition facet 发布，与父目录 typed members、schema hash 和 source lineage 在同一 publication 批次一致捕获，再由既有 activation／Saver 正文存储冻结与恢复。已选 guidance 正文与完整目录定义分离，不把全部 schema 自动塞入模型上下文。首次搜索及进程重启后的精确查询均只读所属冻结正文；只有 hash 而缺少正文时明确失败，不等待第一次搜索结果才保存定义，不读取 live 同名工具补齐。

MCP 输入按其声明的 JSON Schema 方言和完整契约校验；未声明方言按 MCP 支持的默认规则处理，无法支持时明确拒绝目录。properties 外字段仅在 schema 禁止时拒绝，合法扩展字段原样保留。Pydantic 本地工具维持公开模型约定的默认值／转换，不改变已授权的外部行为。JSON Schema 错误作为真实配对参数错误返回，不被宽泛异常吞掉。

底层保留 MCP SDK。若 `langchain_mcp_adapters` 无法保持完整 schema／结果／执行身份，就将现有 MCP 适配路径整体换成 SDK list/call 的薄适配；不同时保留两条生产发现或执行路径，不自行实现协议传输。

### 3. 自动注入、显式发现与执行权限分离

复用 ToolPolicyResolver 的完整解析结果：`model_visible` 决定自动注入，`execution_enabled` 决定执行；显式搜索可以返回已允许执行、仅关闭自动注入的目标，不修改 selection 或全局开关。`restrictions.model_hidden`／`model_visibility_locked` 是独立硬隐藏，任务搜索、精确查询、能力导航及压缩后定义恢复均不得重新投递其定义；执行禁用目标也不经搜索恢复能力。策略身份复用同一 ToolMetadata，不通过描述猜测分组。历史原件不改写；硬隐藏本身不被扩大解释为执行禁用，真实调用仍独立检查当前执行策略。

默认模型上下文只自动提供有界能力导航和发现用法；用户显式打开 `model_visible` 的目标仍自动获得完整定义。按需搜索结果作为正常 ToolResult 保存，并登记其不可变定义 provenance。CSM／Saver 复用已有资源存储恢复活动定义，原 ToolResult 已在有效上下文中时不重复注入整份正文；压缩后只恢复需要保留的当前定义，来源仍为原冻结资源，不建第二套正文库。

目录与已选定义使用同一个 publication/activation 事实。模型看到旧定义时只能得到旧 binding 对应的执行或明确失败；更新在下一允许边界提供完整替换定义／删除提示，不能只改 dispatcher。schema 切换使用完整定义替换，避免模型靠文本 diff 拼接契约。

沿用 `turn|model_call` 配置，不因搜索、授权完成或通知改写原冻结。原工具调用先提交配对的等待结果，原执行按既有终态结束，等待事实由 BackgroundTask 承载，不新增 Job 状态；操作验证成功、目录发布且 continuation 仍有效后，经 RuntimeSourceDispatcher.submit 以稳定幂等 key 和 `after_turn` 策略提交独立 internal execution。该执行 `turn_id=None`，按已有 turn 策略针对自身真实 execution 首次冻结新目录，不借用或伪造用户 Turn，不将恢复伪装为旧 Turn retry/resume。由此两种策略均能合法自动继续，无需用户再说“继续”；未就绪或未过准入时保留等待事实，不重复唤醒空转。搜索本身不成为激活 writer，已有 model_call 边界仍按原合同生效。

固定 `search_tools`／信封的 description 不枚举动态 Server 或目标。上线首次加入 `search_tools` 属于真实 Provider ToolSet 变化，沿用既有 epoch 机制；其后目录、连接和已选定义更新不改变 Provider schemas。Provider 执行绑定 hash 与请求正文 hash 继续分离。

#### 活动定义恢复的唯一链路

活动集合由实际选中的成功 `search_tools` canonical ToolResult 及已封存的 `extension_tool_definition` 来源派生，不另建可变活动表或 schema 库。每项保存原发现的 model-call、tool-call、activation、resource 与 revision；恢复先按原请求及历史 scope 验证冻结正文，不能只按同名工具或当前目录补齐。原内联结果仍被选中时不重复投递；普通 `needs_read` 续读保持 artifact 引用，压缩或 fork 使原引用不可用时恢复必要完整正文。同版本恢复保留原 provenance，新允许边界的定义更新提供完整替换，删除或禁用提供无旧 schema 的事实。

压缩前选集取真实 compaction view 的父选集及同次新提交的 canonical 成员，不扫描所有展示历史或完整原 messages channel。这样既覆盖搜索刚提交、尚未进入常规模型 assembly 就被压缩的边界，也不会重新激活更早已被压缩掉的发现。rewind 按既有 canonical cut 裁剪已封存定义来源；新 epoch 只接收本次经过硬隐藏过滤的 batch，不直接复制旧定义帧。

来源继续走既有 contribution、prefix 和 assembly seal 事务；同一事务验证原发现请求、资源归属、正文 hash、tail-only user 资格及 applied 前驱，不写 CSM 控制行。fork 的原有正文依赖闭包增加这些 typed 引用，将原发现请求、canonical backing 和 activation 一并复制；它们不扩大目标可见前缀，也不恢复旧调用或在线回读源 thread。来源登记前后核对同一 canonical/view manifest，推进冲突明确失败。结构未变化，不提升数据库 schema 版本。

### 4. `connections.manage` 作为本地信封内控制目标

该目标通过现有本地扩展注册链路进入统一目录，凭正常 execution policy 和 HITL 执行；控制 owner 独立于待授权 MCP 连接，确保远端未连接时仍能请求本地授权。

启动及资源冻结必须支持合法的 `authorization_required` 状态：只有 SDK／协议确认且本地支持的授权流程才进入该状态，不能将任意 401、配置或协议异常吞掉。唯一目录仍发布完整的本地控制成员、可用 Server 成员及每 Server 真实 readiness；需授权的远端不被伪报 ready，也不使整个父目录 unavailable。未知远端目录不虚构成员，已知定义不携带可用执行资格。需授权的一个 Server 不得阻断本地控制或其它就绪 Server；非授权故障仍沿明确错误合同处理，不作通用降级。

使用带判别字段的参数模型：

| operation | 输入 | 结果 |
| --- | --- | --- |
| `list` | 可选 server 过滤 | 当前工作区可管理连接摘要 |
| `connect` | 已配置 connection/server identity | 持久操作 ID、状态和产品授权入口 |
| `status` | 操作 ID、可选 `wait_timeout_ms` | 真实状态／终态／等待激活信息 |
| `disconnect` | 连接 identity | 撤销新准入并报告实际关闭结果 |

连接 identity 带 workspace owner、稳定 server identity 和必要的账户标识；token、socket、授权尝试 ID 不作为稳定连接身份。操作 ID 从现有工具调用 operation identity 派生，重放同一次 connect 返回同一有效操作，不反复打开授权页面。超时状态查询不取消连接操作，取消所属 thread 的等待也不自动断开共享工作区连接。

MCP Server 授权由本地连接 owner 使用标准协议／SDK 支持实施；上游应用授权由已连接 Server 的原生工具承担，不假定任意 Server 有 Composio 的应用目录或管理 API。结果区分 `authorization_layer=server|upstream_app`。未实现的授权方式返回 unsupported-auth-method；不把裸 401 当成可自动完成的 OAuth。

连接 ready 要经过授权验证、协议 initialize 和目录验证，不以用户点击完成或 callback 到达为成功。工作区连接状态与执行能力分开：例如 `not_connected|authorization_required|connecting|ready|failed|disconnected|reconcile_required`；授权操作使用 `pending|waiting_user|verifying|succeeded|failed|expired|cancelled`，二者不互相伪造。

凭据经环境变量／安全凭据引用交给实际 owner，不进入 schema、配置明文、ToolResult、SSE 或会话快照；OAuth 交换信息和 token 存放 owner 的受限凭据存储。模型仅获得非敏感操作摘要与产品授权入口；带 state／短期能力的实际授权地址由 UI 按用户身份读取，不持久复制到模型历史。回调使用标准 state/PKCE 与 issuer 校验。

### 5. 工作区领域状态与精确 thread 等待各有归属

连接配置仍属于 Workspace JSONC；实际连接／授权操作在现有工作区基础设施领域存储中维护，不回写 JSONC、不塞入导航 catalog 或 Gateway 全局库。凭据与业务状态分离。

等待记录复用现有后台工作存储／handler 和内部执行准入，按 `(session_id, thread_id, operation_id)` 关联到既有会话节点；具体文件由统一路径解析器取得，不手拼日期或全局目录。记录至少包含 deadline、操作／连接引用、最后观察状态、通知／恢复去重身份和待恢复 continuation 引用，不存储可执行代码或复制业务参数作为第二权威。

采用「登记等待 → 读取当前权威状态 → 订阅／消费变化并再核状态」语义，避免授权在登记前完成造成丢唤醒。连接 owner 确认终态并持久提交后，再发送只含操作引用与状态的最小事件；不能单凭事件把连接标为 ready。等待 handler 只观察和对账授权，不重复执行 connect、token 交换或业务写入；复用现有 typed/versioned handler 与原子 mailbox/checkpoint effect，不把观察等待误标为结果未知的外部副作用。

完成通知只进入原 thread 邮箱。在既有 SessionControlStore 事务中原子提交 checkpoint、稳定 message_id 的邮箱消息及 checkpoint 内待投递 continuation 引用，再由可恢复投递步骤调用 RuntimeSourceDispatcher.submit，成功后结算投递阶段；崩溃后用同一幂等 key 重放，不新增第二套唤醒队列。邮箱 emit／collect 不代表 Job 已准入；邮箱消费 ack 仍随 canonical ToolResult 提交，内部 wakeup ack 沿现有执行合同，不能互相代替。

仅对仍有效、未超时／取消、通过当前权限及 Session 删除 fence 的 continuation 请求内部唤醒。重复 callback／重启重放至多生成一次逻辑恢复效果；用户输入与唤醒交错仍经现有执行队列。完成授权不会自动重发结果未知的外部写入；需要重新查询真实状态或新的明确执行意图。

短等待复用既有总工具期限／取消 child scope，不突破其 deadline。长等待不维持长工具调用、不靠模型反复轮询。重启时重建连接而非恢复旧 socket；核实授权操作状态，无法确认时标为 reconcile_required／明确失败，不自动重放历史副作用。

授权等待注册为既有 BackgroundTask 后，复用 BackgroundTaskResourceProvider 的展示、取消和对账入口，不建立独立授权任务清单。活动等待继续遵守现有 thread idle unload blocker；冷重建验收指进程重启后的恢复，不承诺活动任务可无条件卸载，也不新增未定义的 suspended 状态。

### 6. 唯一 MCP owner 内按 Server 监督连接与调用

每个工作区实例装配一个 MCP owner，内部每 Server 一个拥有 session 上下文的监督 task；同进程多工作区的 server ID 相同也不能共用运行态。task 必须在相同生命周期上下文中创建、排空、关闭 MCP/AnyIO session。用现有 LifetimeScope 管理进程内 task，但连接是否停止由领域 owner 决策。

生命周期实现由 `session_runtime.py` 持有每 Server 的原 task，LifetimeScope 登记其关闭回调并等待原 task 退出，不在 release task 中退出 SDK CancelScope。目录 owner 仍独占完整 relist、通知 fencing 与发布；连接 task 的异常会失效已发布目录，启动取消和部分失败排空实际创建的上下文，释放失败阻止重开并保留原异常。连接代际在同一 Owner 重启时单调推进；该生命周期切片不代替后续调用 handle、持久 lease 或配置共同提交协议。

区分三类身份：

| 身份 | 推进条件 | 用途 |
| --- | --- | --- |
| catalog revision／目录 generation | 目录语义变化 | 定义冻结与内容版本 |
| connection generation | session 新建／替换 | 远端执行身份、迟到 callback fencing |
| invocation lease | 每个已接纳调用 | 排空、期限和取消归属 |

跨 execution／Turn 的持久资源持有复用唯一 ExternalResourceLeaseLedger 的 `mcp_connection` 与 typed resource-use 记录，真实状态仍由连接 owner 更新。owner 内 generation/handle 的短期调用保留只管理本次执行准入与排空，不另建持久资源账本，也不按任意参数名推断资源占用。

目录全量 relist 仍只有一条路径；支持 list_changed 的 Server 消费通知，其余在显式刷新／重连边界更新。无语义变化不推进目录 revision；相同 schema 的连接替换仍推进连接 generation。可调用记录包含 server identity、connection generation、完整 schema hash 与可审计 handle 引用；由 owner 解析，Agent 不再捕获 initial_targets 或回退 live publication。

旧 model-call 已封存但尚未执行的声明应有有界的 generation 保留／执行准入引用，覆盖 HITL 等待；原 handle 到期或不可恢复时，按原 tool_call_id 显式失败，不无限留存连接。正常替换只排空切换前已接纳或已保留执行资格的调用；切换后不向旧 generation 授予新资格。禁用／断开立即撤销尚未启动调用的执行资格，进行中调用按声明的取消策略收敛。断线／超时无法证明远端未执行时返回 outcome-unknown，不自动重试有副作用调用。

封存资格沿原 ExternalResourceLeaseLedger 登记，与实际发出的 operation lease 分开结算，不能把释放资格当成远端操作已结束。`mcp.supervision.sealed_call_timeout_seconds` 默认1800秒，允许1–86400秒，沿既有 Workspace 覆盖层合并；当前修改仍需重启工作区。从模型发送前取得原 handle 开始计时，包含模型生成和人工确认等待，同一封存资格不得重试续期。响应后只保留真实输出的信封调用身份，显式 HITL edit 可选择原封存目录内其它目标；未使用的本地调用／拒绝／直接应答资格也不得超出原期限。重启丢失的 handle 不可恢复或迁投新连接。正常配置热替换与提前结算未使用资格继续由4.4及7.x接通，不能以当前有界资格代替热替换验收。

输出通过既有结果存储与媒体／artifact 合同保存，保留 MCP isError、结构化内容和多模态引用；progress/cancel 作为调用事实转接，不把每条进度当新的工具结果或成功通知。

调用进度复用原 `resource.operation` Activity，以受信 operation ID 和封存 connection_ref 关联；只转发数值 progress／total，不把 Server 自由 message 混入公开状态。SDK 在接收 task 执行 callback，适配器须捕获原调用 scope；进度写入失败必须交还原调用，不能被 SDK 的日志处理吞掉。

stdio 和 Streamable HTTP 统一使用官方 SDK transport 与同一个可取消 session。工具取消或 SDK 请求超时按原 RPC request ID 投递 `notifications/cancelled`，投递有界且 shield 排空；成功投递只证明通知已发送，不证明远端停止或回滚。取消、超时或断线无法确认结果时，返回唯一 typed `mcp_outcome_unknown` 回执及 artifact，明确 `effect=unknown`、`retryable=false` 和先核实远端状态的恢复要求；取消仍交还原 scheduler。实时终态、request carrier、canonical attempt 和冷检查点保留相同回执，checkpoint 不得仅从 ToolMessage.status 将 unknown 降为确认失败。request projection 与实际 attempt 沿用各自身份，不合并为一条。此调用状态只在本次 SDK 调用内使用，不代替持久连接 lease 或已封存执行资格。

### 7. 配置热替换接入既有整体候选流水线

保持 Workspace 有效配置覆盖顺序；只处理最终有效值变化。服务器 command/args/env/cwd、URL/headers/auth 引用、enabled 和增删在 supervisor 验收后按 Server 对账；未覆盖或不能安全替换的依赖仍明确归入 restart policy，不把整个 `/mcp` 无条件改成任意字段热更新。

候选步骤：预检并持久 applying → 所属 task 准备变更的连接 → initialize／完整 relist／命名冲突检查 → 在现有配置 owner 的提交协议下切换 runtime 指针与目录 publication → active promotion 确认 → 排空退役连接。多个 Server 同一候选全部准备成功才整体可见；网络准备不持有 publication 锁。

发布与配置 active promotion 使用同一受控准入屏障／提交标识：模型 preparation、API 和工具调用不能观察到 candidate 目录配旧 active 配置的中间状态；不能只依赖两个各自原子的赋值。崩溃后依据持久提交阶段对账，不声称 SQLite 能回滚外部 session。

接线复用既有配置 applying journal、claim 和 promotion owner。候选应用器在原 shadow scope 中准备资源并返回同步激活回调，不在准备阶段公开新连接；applying journal／claim 先于候选资源准备建立，SQLite promotion 与快照、shadow 及运行时激活共用同步发布屏障。配置读取、MCP Owner 和 Registry／调用准入复用同一工作区锁，不在锁内执行网络准备或旧连接排空。候选回收失败保留原 scope 并阻止下一次替换，提交后的激活或清理失败保留实际新 active，不伪称回滚；不新增配置事实源。该共同发布基础仍须接入逐 Server 候选、准入 fence 与退役排空，完成相应真实验收后才能解除 MCP 重启策略。

允许并行启动的 Server 先准备新连接；单实例 stdio Server 采用可观察的 stop-then-start，暂时关闭该 Server 的执行准入。前者失败保留旧 active 且明确报告 apply_failed；后者无法恢复旧服务时报告 recovery_required，不伪称旧运行时仍健康。已承诺旧调用的 handle 无法维持时真实结束／失败，绝不迁投新同名工具。

分阶段交付时，先开放 HTTP Server 的候选准备、增删／禁用和共同切换；涉及 stdio、监督参数或其它需重启字段的混合候选仍整体进入 pending_restart。退役连接从切换时开始，最多按原 `sealed_call_timeout_seconds` 排空本地 active 占用，关闭等待另有相同上限；原封存资格继续服从各自更早的期限。账本中的结果未知操作不延长 socket 保留，也不因 socket 关闭而被结算。候选期间或提交前收到新通知时不能发布过期的 available 目录，必须保持明确不可用直至完整刷新。配置状态的 applied_paths／deferred_paths 从当前 candidate／attempt 的原配置事件读取，不维护第二份可变结果。

turn-bound MCP 目录的原执行在首次模型发送前保留精确连接资格，由原 TurnExecutionScope 持有并在原账本登记。后继模型调用只可继承该原执行已经保留的 handle 与原期限，不因切换后产生新的 model_call_id 重新计时或扩大目录；原执行结束、期限到达或禁用／撤权后拒绝继承。model-call-bound 目录继续逐次取得当时 active 连接。此执行持有只管理短期资格，不复制模型正文或创建第二套事实来源；进程重启不能恢复旧 socket。

快速重启可能早于原配置 apply claim 到期。原 ConfigService 在保持已确认 active 的同时，按该 claim 的持久期限安排启动对账；不提前抢占有效 fencing，也不把一次启动扫描当作完成恢复。到期后由原 store 标记未提交 journal 为 recovery_required、回收过期 claim，再经原 reload 锁与整体候选流水线建立新的验证／提交 attempt，保留原未提交历史。若 promotion 已提交，则从持久 active 建立新连接，原 journal 和配置事件保持 committed／applied；遗留 claim 的到期回收不依赖还有 applying 候选。对账任务归配置生命周期关闭，失败保留日志及原存储事实，不建立第二套恢复队列。

切换后的旧连接清理失败是可观察的 draining/cleanup failure；不能把已经切换的配置说成「从未应用」，也不能伪报资源已释放。后台 owner 持续记录实际状态并阻止冲突替换，处理结果沿用现有配置／活动事件合同。

新增配置只扩展 Workspace 既有 schema、默认与必要模板，使用功能域／子系统／参数层级；不新增凭据明文字段或平行配置文件。现有配置字段不改名；确有持久契约变化时显式版本化并一次性迁移，不加永久兼容读取。

### 8. UI 与外部调用保持原边界

Gateway 仅透明路由 `/api/v1/*` 连接业务 API 和事件，维持同一 request_id；连接 owner 位于目标工作区。远程工作区授权页面打开在用户客户端，但回调必须到实际 owner 可达的地址；不具备回调通道时明确报告不支持，不误用客户端 localhost。

授权入口及等待状态显示在当前会话流程；连接列表／管理属于工作区层级，展示入口位于主窗口的底部面板或既有工作区设置。保持浏览器客户端既有术语和数据归属；不扩展 Electron／移动原生实现。用户关闭授权页不等同于连接成功。

普通并发以同一轮多个既有信封调用表达，每个保留独立 ID、冻结 binding、HITL、scope 和结果；依赖关系通过后续模型步骤推进。保留既有 `tool_name+arguments` 输入，不引入 batch 数组第二形状或多套 dispatcher。

### 9. 配置规格按明确依赖顺序同步

`unify-product-runtime-logging` 必须先同步主规格并完成归档，本变更随后才能同步／归档。两个配置 capability 的同名 MODIFIED Requirement 使用包含日志和 MCP 合同的完整正文：保留日志归 Gateway `logging.runtime`、仅日志热应用和混合 Gateway 候选整体提交场景，仅将 MCP 重启分类改为按安全替换验收结果判断。

同步前重新读取日志归档结果与最新主规格，核对双方全部场景；日志尚未归档或同名合同又变化时，停止本变更的规格同步／归档并先更新本 delta，不允许逆序覆盖。此依赖只限制规格同步／归档，不阻塞相互隔离的实现切片；本变更不修改日志 change 或 Gateway 日志实现。

## Risks / Trade-offs

- 信封无法让 Provider 对内部目标参数施加逐工具生成约束 → 提供完整定义与严格本地校验，统计首次成功率再决定是否需要独立 Provider 优化。
- 任务搜索可能漏选或多一轮 → 确定性排名、精确查询与少量候选；不把建议流程当保证，不自动执行搜索结果。
- 授权完成与原 Turn 结束交错 → 复用 after_turn 的独立内部执行与新 execution 冻结，未满足准入时显示等待事实，不改原冻结或默认策略，不重复唤醒。
- 热替换无法保持单实例旧调用 → 明确短暂停用与失败结果；对每种替换方式测真实入口，不承诺无中断。
- 目录版本相同但远端行为改变 → 本地 generation 只能约束连接身份，不能保证第三方实现语义不变；不自动重试结果未知写入。
- 授权等待可能被误当一次性执行许可 → 授权与行动确认分离，恢复时检查最新策略与原意图，重复事件不重复副作用。
- 规划并行归档可能覆盖配置文字 → 明确日志先归档、本变更后同步的前置门；本 delta 保留日志完整正文与场景，同步前以最新主规格核对，禁止逆序覆盖。

## Migration Plan

### MCP 结果保真切片的集成边界（2026-10-26）

- 固定信封统一调用目标 BaseTool 的 ainvoke，删除直接调用 func／coroutine 的分路；保留目标的同步／异步执行、response_format、handle_tool_error、调用 ID 和 artifact，仅隔离内层事件回调。受信调用身份与完整公开参数以位置参数分开传入，config、run_manager、callbacks、tool_name 和 tool_call_id 等合法字段不被框架注入覆盖。
- 唯一 SDK 适配保留 isError、structuredContent、图片、音频、资源链接和嵌入正文。真实调用返回配对 ToolMessage，业务错误不通过异常路径丢失媒体或结构化结果；无调用 ID 的独立字典调用继续遵循既有 BaseTool 正文返回合同。
- 真实产品验收发现上游 ToolNode 的固定内容白名单遗漏标准 audio 块，会将整组内容转成 JSON 字符串。现有 graph_tool_adapter 通过继承替换结果规范化，保留同一工具执行、注入、并发、中间件和错误处理；普通 ToolMessage、Command 和混合列表均保留正文与唯一配对终态校验，不修改第三方常量或对象原型。
- 实时 completion、canonical attempt、request carrier、codec 和冷检查点恢复贯通完整正文与 JSON artifact；SSE 继续使用派生文本。跨载体等价判断比较原正文和 artifact，同文字但不同媒体不得复用。未新增数据库结构或第二份结果存储。
- 后继模型请求的工具正文独立于 AI 思考过滤。Chat Completions 使用官方媒体编码，将媒体按原调用 ID 放到连续工具结果之后的具名数据消息，保持工具配对相邻；文件 URL 作为资源引用文本。Responses 的直接历史与 native seal 共用纯投影，明确提示每种媒体并保留资源 URL。wire projector 升为 v3，历史原件及其 preimage/hash 保持可读，不重写旧请求。
- 真实本地 HTTP 服务复用 model_stream 的响应资产与协议编码，分别验证 Chat／Responses 和同步／异步入口；真实网络正文、封存 request_hash 与冷恢复必须一致。它与 Gateway 产品入口验收承担不同证明职责，不删除真实网络验证。
- 本切片的产品证据使用真实 Gateway、Session／消息入口、真实 stdio Server 和 model_stream 驱动并发成功／失败调用，核对封存请求 hash、canonical 归属与冷读取。回放不证明外部模型服务接受所有媒体协议；进度、取消／断线的 outcome-unknown、连接资格与持久 lease 仍按 4.2、4.4–4.6 完成，4.5 不提前勾选。

### 精确连接执行身份切片的集成边界（2026-10-26）

- MCP Owner 为每次连接生成包含 Owner 实例与单调连接代际的不可变 connection_ref，写入现有 typed member revision；目录语义 revision 和 catalog generation 仍仅反映完整定义变化。父目录与 definition facets 原子携带相同成员事实，沿原 activation／Saver 持久化，不增加表、writer 或第二份成员集合。
- binding／dispatch preimage 和 lineage 升为 v3，binding ID 同时覆盖完整成员身份，防止同 schema、同目录代际的新 Owner 与旧封存身份碰撞。Provider ToolSet、实际 wire bytes、request_hash 及 prefix epoch 不因执行身份变化而改变；历史 v1/v2 原件继续可读，缺少精确连接身份时拒绝恢复执行，不从语义版本推导 socket。
- 完整定义读取不再借构造 dispatch 验证历史；父行、lineage与facets沿同一只读校验恢复。v2完整定义仍用于历史查看和压缩后的恢复，v3才允许解析执行；恢复不改写历史快照，不以旧定义可读推导旧连接可调用。
- Agent 删除 initial/live 两段 resolver。正式信封不再长期持有初始 MCP 工具，统一由 Owner 核对原连接、完整 schema 和实际运行状态，本地扩展仍走原本地注册；执行点继续读取最新独立权限。Registry 捕获同时核对当前成员身份，不能仅凭相同语义 revision／catalog generation 接受旧连接事实。
- 本切片只完成精确解析与旧连接明确失败，不提前实现正常热替换的有界资格、HITL 排空或跨 execution 持久占用。这些继续由4.2／4.4及配置替换切片接入原 Owner 和 ExternalResourceLeaseLedger，不以本次拒绝旧连接代替完整替换合同。

### 有界导航与自动定义切片的集成边界（2026-10-26）

- 删除旧 MCP 全量参数摘要 producer 及其公开符号；统一指引只给有限分组、数量与能力示例，用户显式开启可见性时才投递完整定义。自动定义直接使用同批 definition facet 的规范正文，硬隐藏和执行禁用同时约束导航与自动投递。
- 唯一 publisher 按单次原子选择读取解析全部扩展策略，本地扩展不再因最初关闭执行而永久缺席后续目录。ToolSelectionStore 提交后通知当前 Agent generation，在原 MCP Owner 发布锁内整体重发父行、members 和 facets；订阅随既有 LifetimeScope 释放，不在 preparation 联网或另建目录。
- 验证覆盖 1200 个目标的有界确定性导航、完整组合 schema、软／硬隐藏、开关提交、真实 freeze/seal、turn/model_call、重复投递和冷 Saver；正式 Session／消息入口沿 model_stream 执行 Skill、搜索及真实 Node 调试。Provider ToolSet 与 epoch 保持稳定，指引变化只影响实际请求正文。活动定义的压缩后恢复仍按 3.6 推进，不把本切片视为 3.4／3.7 或连接监督完成。

### 冻结定义与搜索切片的集成边界（2026-10-26）

- 唯一 publisher 在同一 Registry publication 锁中发布 guidance 父行与每目标 definition facet，沿既有 activation／Saver 保存正文、members 和 lineage。搜索恢复时逐项核对来源、完整 schema hash 与原冻结正文；缺失定义明确失败。
- 固定 search_tools 与信封共同保留在 Provider 工具面。查询排名、精确顺序、逐项错误、目录／有效策略／查询绑定分页和预算在同一搜索链路实现；策略身份由目录和执行共用的 metadata_for_tool 生成，软隐藏可发现，硬隐藏及执行禁用明确拒绝。
- 响应为单行规范 JSON，字节预算同时受 search_tools 策略及现有 ToolOutputStore 限制。大定义经该 store 的同一不可变写入方法物化，返回 needs_read、普通读取 URI、正文摘要和冻结 provenance；内联完整定义标为 inline_complete。原有大输出预览也复用该写入方法。
- 定向 matrix 包含真实 stdio、原样参数收据、实际输出中间件、冷 Saver、fork／恢复和两种激活边界；正式 Session／消息入口另以 model_stream 驱动真实调试动作，核对发现结果、ToolSet、request_hash 和前缀。连接控制、默认有界导航、自动完整定义注入及压缩后的活动定义恢复继续按 3.1、3.4–3.7 和后续切片推进。

### 完整 schema 切片的调用与集成边界（2026-10-26）

- `model_tool_schema.py` 统一公开契约导出、JCS 规范值、标准方言及本地引用验证；本地 Pydantic 仍保留默认值、转换与 extra 策略，后端注入字段不能借 extra 进入。
- MCP 写入从唯一 `McpCatalogOwner` 的 SDK 完整分页进入；`tool_adapter.py` 直接把原参数交给原 session，避免第三方适配器占用 `runtime` 等合法字段。结果转换继续复用现有库，连接监督、lease、进度与完整结果发布仍由第 4 节完成。
- Owner revision、custom publisher、guidance 派生、封存工具面、Agent resolver 和信封注册核验均改用完整公开 schema；删除 `extension_args_fingerprint` 及其 `default=str`／repr 回退。参数摘要仅作展示，不参与契约身份。
- `ext-catalog:v2`、catalog/dispatch hash preimage v2 与 `extension-catalog:v2` lineage 明确隔离旧属性指纹。旧历史保留原件并按原读取入口展示；缺少 v2 来源或持有 v1 binding 的旧调用明确返回 `extension-catalog-unavailable`，不重写旧数据、不从 live 同名工具补齐。
- 已核对 CSM 来源协议、Saver activation typed members、ToolSet/prefix 与共享 Backend 的工作区装配边界；本切片不增加 registry、激活 writer 或持久 schema 表，不修改配置、日志及其他 change 的规划。现有 initial/live resolver 只在本切片迁移 schema 身份，其执行生命周期收敛仍属于任务 4.3。
- 回归覆盖公开 schema、信封、目录/激活、dispatch hash、Agent 装配、工具测试入口，以及真实 stdio handler 参数收据。真实后端信封验收沿 Session/消息入口、model_stream 回放、真实调试动作和持久 assembly 核对 Provider ToolSet、请求 hash 与稳定前缀；它不替代后续搜索、授权及 Web 验收。

1. 先全仓核对 schema、工具目录、信封目标解析、确认、恢复与测试消费者，建立受影响调用清单；不修改其他进行中变更的源码或规划。
2. 单切片完成完整 schema 和校验迁移，移除属性摘要身份与合法字段裁剪；确定历史 binding 的显式不可执行处理。
3. 单切片先将完整定义 facet 接入既有冻结正文，再实现 search_tools、硬隐藏策略、结果物化与压缩恢复，替换默认全量摘要；保留用户显式自动注入和 Skill 调用。
4. 单切片将执行 handle／generation／lease 收归 MCP owner，删除 initial/live 两段 resolver，验证延迟调用与 HITL。
5. 单切片打通首次需授权时的启动／目录准入，再实现本地连接控制、标准授权、UI 和持久等待恢复；使用 checkpoint／邮箱的可靠投递和独立内部执行，复用单一信封、结果、后台资源管理与执行准入链路。
6. 单切片接入 Server 级热替换、配置提交和故障对账；验收通过后才修改对应字段 policy，并同时更新配置相关消费者／测试。
7. 完成实际 Gateway／目标工作区／浏览器授权与真实 MCP Server 验收，再运行针对性回归和差异审查。每个切片的证据与命令按仓库规范保留。

回退以代码／配置版本整体回退并保留运行产物为单位；停止受影响连接并显式重建，不运行新旧 dispatcher、不让旧版本读取无法识别的新 binding 或凭据状态。无法安全降级时报告所需版本与保留路径。
