## Context

动机见 proposal.md。本设计以当前已落地的唯一 owner／Registry／activation／seal 链路为基线，并承接进行中的上下文与 itemized 规格，不另建运行时。

源码事实：

- `mcp/catalog_owner.py` 已完整 relist、消费变化通知并发布不可变目录；现有 connection generation 主要约束迟到通知与 relist，尚不是执行调用的完整生命周期。
- `mcp/tool_guidance.py` 只输出截断描述和参数字段摘要；`catalog_activation.py` 将其作为一个已选正文父行发布，typed members 与 lineage 单独保存。
- 多处 schema 指纹使用 `tool.args`，它仅包含 `properties`；`model_tool_schema.py` 已有完整公开 schema 导出，但字典参数校验会裁剪字段；信封又无条件拒绝 properties 之外的字段。
- `agent_factory.py` 捕获 initial targets，再按 schema hash 查当前 publication，不能用于连接热替换后保证原执行身份。
- `/mcp` 当前为 `restart_workspace`；`workspace_lifecycle.py` 明确缺少由原 task 排空旧 generation 的 supervisor。
- 已有目标级 HITL、工具结果存储、精确 thread 后台消息与内部唤醒。它们可复用，但通用 MCP OAuth、连接操作对账与授权完成接线需要新增实现。

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

结果统一承载 `tool_id`、origin/group、用途、完整 `input_schema`、schema hash、catalog revision、匹配理由、execution/model-visible 状态、readiness、缺失连接和后续控制目标。匹配理由来自确定性匹配事实，不声称已执行任务或保证工具效果。首期仅聚合已有 MCP、本地扩展及本地连接控制目标；内置直接工具仍由现有 ToolSet 管理，日后纳入搜索也须声明实际调用入口。

搜索使用调用所属的冻结目录快照；不在模型请求 preparation 或搜索期间临时访问任意 MCP Server。新发布目录按既有激活边界进入下一可见快照。排序以规范化名称／关键词／描述匹配分数和稳定 ID 决胜；首期不额外调用 LLM 或 embedding 服务。分页绑定目录、策略 revision 和查询身份，变化后返回 snapshot conflict／明确重新搜索指引。

每个返回的 schema 必须完整、自包含，保留 `$defs` 与本地引用；首期不能本地解析的外部引用明确拒绝，不静默访问网络或降为宽松校验。容量不足时少返回候选并给 cursor；单个定义超过上限时返回明确的 definition-too-large 和体积事实，不输出残缺 schema 冒充完整定义。精确请求受同一策略和体积约束。

选择新增入口的原因是任务级能力检索与文件 `grep`／历史 `search_context` 的合同不同。底层复用现有资源身份、目录、结果存储和分页习惯，不为减少名称而修改这些工具的业务语义。

### 2. 完整公开 schema 是唯一输入契约

复用并收敛 `export_model_tool_json_schema`；完整 schema 的规范值进入目录语义 revision、target schema hash、发现／自动注入正文和冻结 binding。仅改变顶层 required、additionalProperties、组合规则或 `$defs` 必须反映到身份中。参数摘要只是派生展示数据。

采用既有 canonical JSON／JCS 摘要设施，不再为同一契约维护可分歧的属性指纹或 `default=str`／repr 容错。binding/hash 合同变更明确版本化；历史 schema／handle 不可恢复时拒绝执行并保留原件，不用当前同名工具补洞。回放可读与继续执行是不同能力，不能为了恢复执行而重写历史。

MCP 输入按其声明的 JSON Schema 方言和完整契约校验；未声明方言按 MCP 支持的默认规则处理，无法支持时明确拒绝目录。properties 外字段仅在 schema 禁止时拒绝，合法扩展字段原样保留。Pydantic 本地工具维持公开模型约定的默认值／转换，不改变已授权的外部行为。JSON Schema 错误作为真实配对参数错误返回，不被宽泛异常吞掉。

底层保留 MCP SDK。若 `langchain_mcp_adapters` 无法保持完整 schema／结果／执行身份，就将现有 MCP 适配路径整体换成 SDK list/call 的薄适配；不同时保留两条生产发现或执行路径，不自行实现协议传输。

### 3. 自动注入、显式发现与执行权限分离

保留现有两项用户控制：`model_visible` 决定自动注入，`execution_enabled` 决定执行。显式搜索可以返回已允许执行但未自动注入的目标，不修改 selection 或全局开关；硬性执行禁用目标不返回完整定义，不经搜索恢复其能力。

默认模型上下文只自动提供有界能力导航和发现用法；用户显式打开 `model_visible` 的目标仍自动获得完整定义。按需搜索结果作为正常 ToolResult 保存，并登记其不可变定义 provenance。CSM／Saver 复用已有资源存储恢复活动定义，原 ToolResult 已在有效上下文中时不重复注入整份正文；压缩后只恢复需要保留的当前定义，来源仍为原冻结资源，不建第二套正文库。

目录与已选定义使用同一个 publication/activation 事实。模型看到旧定义时只能得到旧 binding 对应的执行或明确失败；更新在下一允许边界提供完整替换定义／删除提示，不能只改 dispatcher。schema 切换使用完整定义替换，避免模型靠文本 diff 拼接契约。

沿用 `turn|model_call` 配置，不因搜索、授权完成或通知偷偷突破 `turn` 冻结。授权后连接可用并不等于原 Turn 已获得新工具；如果尚未到激活边界，返回等待激活事实，允许用户或下一合法执行边界推进，不伪造用户 Turn、不重复唤醒空转。搜索发生在已冻结目录上，不单独成为未声明的激活 writer。

固定 `search_tools`／信封的 description 不枚举动态 Server 或目标。上线首次加入 `search_tools` 属于真实 Provider ToolSet 变化，沿用既有 epoch 机制；其后目录、连接和已选定义更新不改变 Provider schemas。Provider 执行绑定 hash 与请求正文 hash 继续分离。

### 4. `connections.manage` 作为本地信封内控制目标

该目标通过现有本地扩展注册链路进入统一目录，凭正常 execution policy 和 HITL 执行；控制 owner 独立于待授权 MCP 连接，确保远端未连接时仍能请求本地授权。

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

采用「登记等待 → 读取当前权威状态 → 订阅／消费变化并再核状态」语义，避免授权在登记前完成造成丢唤醒。连接 owner 确认终态并持久提交后，再经已有可恢复投递机制发送最小事件；事件只含操作引用与状态，不能单凭事件把连接标为 ready。

完成通知只进入原 thread 邮箱；仅对仍有效、未超时／取消、通过当前权限及 Session 删除 fence 的 continuation 请求内部唤醒。重复 callback／重启重放至多生成一次逻辑恢复效果；用户输入与唤醒交错仍经现有执行队列。完成授权不会自动重发结果未知的外部写入；需要重新查询真实状态或新的明确执行意图。

短等待复用既有总工具期限／取消 child scope，不突破其 deadline。长等待不维持长工具调用、不靠模型反复轮询。重启时重建连接而非恢复旧 socket；核实授权操作状态，无法确认时标为 reconcile_required／明确失败，不自动重放历史副作用。

### 6. 唯一 MCP owner 内按 Server 监督连接与调用

每个工作区实例装配一个 MCP owner，内部每 Server 一个拥有 session 上下文的监督 task；同进程多工作区的 server ID 相同也不能共用运行态。task 必须在相同生命周期上下文中创建、排空、关闭 MCP/AnyIO session。用现有 LifetimeScope 管理进程内 task，但连接是否停止由领域 owner 决策。

区分三类身份：

| 身份 | 推进条件 | 用途 |
| --- | --- | --- |
| catalog revision／目录 generation | 目录语义变化 | 定义冻结与内容版本 |
| connection generation | session 新建／替换 | 远端执行身份、迟到 callback fencing |
| invocation lease | 每个已接纳调用 | 排空、期限和取消归属 |

目录全量 relist 仍只有一条路径；支持 list_changed 的 Server 消费通知，其余在显式刷新／重连边界更新。无语义变化不推进目录 revision；相同 schema 的连接替换仍推进连接 generation。可调用记录包含 server identity、connection generation、完整 schema hash 与可审计 handle 引用；由 owner 解析，Agent 不再捕获 initial_targets 或回退 live publication。

旧 model-call 已封存但尚未执行的声明应有有界的 generation 保留／执行准入引用，覆盖 HITL 等待；原 handle 到期或不可恢复时，按原 tool_call_id 显式失败，不无限留存连接。正常替换只排空切换前已接纳或已保留执行资格的调用；切换后不向旧 generation 授予新资格。禁用／断开立即撤销尚未启动调用的执行资格，进行中调用按声明的取消策略收敛。断线／超时无法证明远端未执行时返回 outcome-unknown，不自动重试有副作用调用。

输出通过既有结果存储与媒体／artifact 合同保存，保留 MCP isError、结构化内容和多模态引用；progress/cancel 作为调用事实转接，不把每条进度当新的工具结果或成功通知。

### 7. 配置热替换接入既有整体候选流水线

保持 Workspace 有效配置覆盖顺序；只处理最终有效值变化。服务器 command/args/env/cwd、URL/headers/auth 引用、enabled 和增删在 supervisor 验收后按 Server 对账；未覆盖或不能安全替换的依赖仍明确归入 restart policy，不把整个 `/mcp` 无条件改成任意字段热更新。

候选步骤：预检并持久 applying → 所属 task 准备变更的连接 → initialize／完整 relist／命名冲突检查 → 在现有配置 owner 的提交协议下切换 runtime 指针与目录 publication → active promotion 确认 → 排空退役连接。多个 Server 同一候选全部准备成功才整体可见；网络准备不持有 publication 锁。

发布与配置 active promotion 使用同一受控准入屏障／提交标识：模型 preparation、API 和工具调用不能观察到 candidate 目录配旧 active 配置的中间状态；不能只依赖两个各自原子的赋值。崩溃后依据持久提交阶段对账，不声称 SQLite 能回滚外部 session。

允许并行启动的 Server 先准备新连接；单实例 stdio Server 采用可观察的 stop-then-start，暂时关闭该 Server 的执行准入。前者失败保留旧 active 且明确报告 apply_failed；后者无法恢复旧服务时报告 recovery_required，不伪称旧运行时仍健康。已承诺旧调用的 handle 无法维持时真实结束／失败，绝不迁投新同名工具。

切换后的旧连接清理失败是可观察的 draining/cleanup failure；不能把已经切换的配置说成「从未应用」，也不能伪报资源已释放。后台 owner 持续记录实际状态并阻止冲突替换，处理结果沿用现有配置／活动事件合同。

新增配置只扩展 Workspace 既有 schema、默认与必要模板，使用功能域／子系统／参数层级；不新增凭据明文字段或平行配置文件。现有配置字段不改名；确有持久契约变化时显式版本化并一次性迁移，不加永久兼容读取。

### 8. UI 与外部调用保持原边界

Gateway 仅透明路由 `/api/v1/*` 连接业务 API 和事件，维持同一 request_id；连接 owner 位于目标工作区。远程工作区授权页面打开在用户客户端，但回调必须到实际 owner 可达的地址；不具备回调通道时明确报告不支持，不误用客户端 localhost。

授权入口及等待状态显示在当前会话流程；连接列表／管理属于工作区层级，展示入口位于主窗口的底部面板或既有工作区设置。保持浏览器客户端既有术语和数据归属；不扩展 Electron／移动原生实现。用户关闭授权页不等同于连接成功。

普通并发以同一轮多个既有信封调用表达，每个保留独立 ID、冻结 binding、HITL、scope 和结果；依赖关系通过后续模型步骤推进。保留既有 `tool_name+arguments` 输入，不引入 batch 数组第二形状或多套 dispatcher。

## Risks / Trade-offs

- 信封无法让 Provider 对内部目标参数施加逐工具生成约束 → 提供完整定义与严格本地校验，统计首次成功率再决定是否需要独立 Provider 优化。
- 任务搜索可能漏选或多一轮 → 确定性排名、精确查询与少量候选；不把建议流程当保证，不自动执行搜索结果。
- turn 激活延迟影响授权后继续 → 显示等待激活原因，遵守原策略；评估真实工作流后由用户选择现有 model_call 配置，不隐式改变默认。
- 热替换无法保持单实例旧调用 → 明确短暂停用与失败结果；对每种替换方式测真实入口，不承诺无中断。
- 目录版本相同但远端行为改变 → 本地 generation 只能约束连接身份，不能保证第三方实现语义不变；不自动重试结果未知写入。
- 授权等待可能被误当一次性执行许可 → 授权与行动确认分离，恢复时检查最新策略与原意图，重复事件不重复副作用。
- 规划并行归档可能覆盖配置文字 → 本 change 的配置 delta 仅表达 MCP 的变化；实施／归档时与日志 change 按 requirement 合并，保留它的日志结果。

## Migration Plan

### 完整 schema 切片的调用与集成边界（2026-10-26）

- `model_tool_schema.py` 统一公开契约导出、JCS 规范值、标准方言及本地引用验证；本地 Pydantic 仍保留默认值、转换与 extra 策略，后端注入字段不能借 extra 进入。
- MCP 写入从唯一 `McpCatalogOwner` 的 SDK 完整分页进入；`tool_adapter.py` 直接把原参数交给原 session，避免第三方适配器占用 `runtime` 等合法字段。结果转换继续复用现有库，连接监督、lease、进度与完整结果发布仍由第 4 节完成。
- Owner revision、custom publisher、guidance 派生、封存工具面、Agent resolver 和信封注册核验均改用完整公开 schema；删除 `extension_args_fingerprint` 及其 `default=str`／repr 回退。参数摘要仅作展示，不参与契约身份。
- `ext-catalog:v2`、catalog/dispatch hash preimage v2 与 `extension-catalog:v2` lineage 明确隔离旧属性指纹。旧历史保留原件并按原读取入口展示；缺少 v2 来源或持有 v1 binding 的旧调用明确返回 `extension-catalog-unavailable`，不重写旧数据、不从 live 同名工具补齐。
- 已核对 CSM 来源协议、Saver activation typed members、ToolSet/prefix 与共享 Backend 的工作区装配边界；本切片不增加 registry、激活 writer 或持久 schema 表，不修改配置、日志及其他 change 的规划。现有 initial/live resolver 只在本切片迁移 schema 身份，其执行生命周期收敛仍属于任务 4.3。
- 回归覆盖公开 schema、信封、目录/激活、dispatch hash、Agent 装配、工具测试入口，以及真实 stdio handler 参数收据。真实后端信封验收沿 Session/消息入口、model_stream 回放、真实调试动作和持久 assembly 核对 Provider ToolSet、请求 hash 与稳定前缀；它不替代后续搜索、授权及 Web 验收。

1. 先全仓核对 schema、工具目录、信封目标解析、确认、恢复与测试消费者，建立受影响调用清单；不修改其他进行中变更的源码或规划。
2. 单切片完成完整 schema 和校验迁移，移除属性摘要身份与合法字段裁剪；确定历史 binding 的显式不可执行处理。
3. 单切片实现 search_tools、冻结发现结果、策略与压缩恢复，替换默认全量摘要；保留用户显式自动注入和 Skill 调用。
4. 单切片将执行 handle／generation／lease 收归 MCP owner，删除 initial/live 两段 resolver，验证延迟调用与 HITL。
5. 单切片实现本地连接控制、标准授权、UI 和持久等待恢复，复用单一信封、结果、后台与执行准入链路。
6. 单切片接入 Server 级热替换、配置提交和故障对账；验收通过后才修改对应字段 policy，并同时更新配置相关消费者／测试。
7. 完成实际 Gateway／目标工作区／浏览器授权与真实 MCP Server 验收，再运行针对性回归和差异审查。每个切片的证据与命令按仓库规范保留。

回退以代码／配置版本整体回退并保留运行产物为单位；停止受影响连接并显式重建，不运行新旧 dispatcher、不让旧版本读取无法识别的新 binding 或凭据状态。无法安全降级时报告所需版本与保留路径。
