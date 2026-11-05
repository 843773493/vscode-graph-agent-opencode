# context-injection-lifecycle Specification

2026-11-04 范围说明：团队／成员／委派与跨 Session 通信的新设计和专项验收已移至本 change design 的延期参考，不属于本轮发布合同。以下 main/child、FIFO、来源及 owner 隔离仍约束现有通用执行链；出现 team／delegation 类型不授权扩展该功能。归档按 tasks 当前本轮 checklist 与实际证据，不能发布延期设计为已实现能力。

## Purpose

为所有上下文 mutation 建立单一持久化/assembly owner，为动态 source 建立可审计且可重放的注入生命周期，并保证正常追加期间已提交 wire context 前缀字节级稳定，使 Skill 加载、文件变化、ToolSet hard rebase、rewind、compaction 和 Provider 投影不会重复、重排或伪造真实用户 Turn。

## Requirements

### Requirement: 已提交上下文前缀必须保持字节级稳定

系统 SHALL 把 Provider profile 下按 plan ordinal 排序并序列化的 wire context item frames 作为稳定前缀边界。在同一 prefix epoch 内，新 sealed assembly 必须完整保留父 assembly 的 context bytes，并且只能在其后追加新 item；不得修改旧 item 的 role、正文、content block、空白、编码、顺序或序列化。同一 epoch还必须绑定完全相同的 Provider projection profile、ToolSetRef和影响 root/tool visibility的 policy hash。首次组装、实际发生的上下文压缩重建、rewind重建和 ToolSet hard rebase可以建立重建前缀的新 epoch；此外，模型路由明确选择不同 Provider profile（包括真实失败后的 fallback）时，MUST 以 `provider_changed` 建立仅重投影的 epoch，复用上一 sealed assembly 的正文、角色、来源身份及顺序，不重新观察来源、编译 root、刷新 view 或伪造 ToolSet change。

#### Scenario: 普通 source 更新只追加后缀

- **WHEN** 已提交 assembly 后 AGENTS、Skill、团队状态或其它 runtime source 发生变化
- **THEN** 下一 assembly 的已提交 context bytes 以旧 assembly 的完整 context bytes 为前缀，并只在尾部追加新的不可变 source item

#### Scenario: 普通 canonical item 只追加后缀

- **WHEN** 真实用户消息、assistant/reasoning、tool call或配对 tool result在当前 ToolSet与 active view下提交
- **THEN** owner保持当前 prefix epoch，并按 canonical ordinal只在父 assembly尾部追加对应不可变 item frames

#### Scenario: 重复 dispatch 复用同一字节

- **WHEN** 同一 sealed assembly 因 Provider transport 或进程恢复而重试
- **THEN** 系统复用已封存的 item 顺序和 Provider serialization，不重新投影、合并或格式化上下文

#### Scenario: 稳定前缀校验失败

- **WHEN** 新 assembly 的父前缀长度、hash 或逐字节内容与已提交父 assembly 不一致
- **THEN** 系统阻止 Provider dispatch 并返回明确的 stable-prefix violation，不以 cache miss 或重新组装静默继续

#### Scenario: 已组装 thread 切换 Provider profile

- **WHEN**SessionThread已有sealed assembly，用户或控制面把desired model/provider projection profile改为不同值，但既没有本次实际模型路由的明确选择，也没有实际compaction、rewind或有效ToolSet hard rebase
- **THEN**owner保持原applied profile和assembly，返回`provider-profile-change-requires-rebuild`且不dispatch；不得静默继续使用旧模型、绕过路由授权创建epoch或用no-op ToolSet变化伪造rebase

#### Scenario: 跨 Provider fallback 保留冻结上下文

- **WHEN** 实际路由选中的 Provider 在请求或流迭代阶段失败，随后选择不同协议或 profile 的候选
- **THEN** owner 在同一封存事务发布 `provider_changed` epoch 和新 profile 请求，旧 assembly 保持不可变；root 正文及 provenance、已有 canonical/source frame 的正文、角色和顺序完整保留，只转换投影并按原合同追加新 item。失败和成功请求均保留各自可重建的 request hash；工具循环后再次选择候选以及进程重启恢复仍能读取全部历史，不因上次失败的 profile 锁死

#### Scenario: 合法边界应用 pending Provider profile

- **WHEN**desired profile已变化，随后实际compaction、rewind或有效ToolSet hard rebase建立合法新epoch
- **THEN**owner把新profile、对应projection bytes和首个新epochassembly原子applied并保存旧/新profile provenance；initial assembly则可直接采用当时desired profile

#### Scenario: 合法重建边界

- **WHEN** 首次组装、实际上下文压缩、rewind 重建 active view或 ToolSet hard rebase发生
- **THEN** 系统创建新的 prefix epoch并记录 `initial`、`compaction`、`rewind`或 `toolset_changed`原因；新 epoch内的后续请求重新遵守字节级稳定约束

#### Scenario: ToolSet 变化不得沿用旧 epoch

- **WHEN** 下一次模型请求的 ToolSetRef或工具 policy hash不同于当前 applied binding
- **THEN** 系统不得在旧 prefix epoch继续 seal或只追加工具切换文本，必须先执行 ToolSet hard rebase

### Requirement: 所有上下文 mutation 必须经唯一 owner 并保持 domain 分工

系统 SHALL为每个`SessionThread`维护唯一的逻辑RolloutCheckpointSaver/ContextStore mutation owner，处理所有会影响canonical history、active view、source control state、ToolSet binding和sealed assembly的变更。owner key是`(session_id, thread_id)`，其durable identity/state与thread同寿命；resident owner实例可以在idle unload时关闭，再于execution admission按原key恢复，并以runtime generation/lease阻止旧实例写入。Session只保存main-thread路由、thread catalog与共享资源。该边界 MUST区分canonical append、source lifecycle decision、ToolSet switch和compaction/rewind epoch rebuild；共享owner与原子transaction不得把这些domain事实都改造成CSM source。rewind/compaction可先提交active view、版本化CSM control state和`PendingPrefixEpochTransition`，但该pending记录没有wire bytes且不是applied epoch；下一次model-call preparation必须把source reconciliation、首个新epochassembly seal和transition消费原子提交。CSM SHALL仅管理需要identity/revision/diff/tracking/reconciliation的instruction、file和runtime source，不得截获普通canonical item、ToolSet或compaction summary。

CSM/source producer MUST以 typed `SourceObservation`、`ApplySourceLifecycleDecision` 和 itemized `ContextContribution` 传递会影响追踪/激活/选择/替换的核心字段；`selection_role`、`replacement_policy`、`source_binding`、tracking state、desired/applied revision与 owner identity不能隐藏在自由 metadata/extension 中。只有 itemized owner-thread registry 可独占分配/恢复既有 `ContextContribution` 上的 typed `source_ordinal`，CSM不得按观察事件顺序或内存计数补号。（`source_ordinal` 的唯一权威定义与「本 change 不引入 `RegisteredContribution` 这一独立返回类型」的裁定归 `add-itemized-rollout-context`，具名见其 `specs/itemized-rollout-context/spec.md` 的 `source_ordinal` 登记与 `tasks.md` 1.5；本 change 只引用不复述，CSM、watch reaction 与 ledger 同样不得从事件顺序、内存计数或 `extensions` 补造该序号。）命名空间化、版本化的 `extensions` 可以透传或审计，但不参与 tracking、diff、selection、role、epoch、ToolSet或dispatch 决策；未知扩展不能因字符串 key 恰好等于旧控制 flag 而改变行为。任何新增生产代码需要影响核心行为时，必须先声明新的 typed capability/intent、owner、校验和测试。恢复时核心字段缺失或不一致必须显式失败，不从当前资源快照或扩展猜测；旧实验字段形态不提供 runtime alias/双读。

#### Scenario: 提交真实用户输入

- **WHEN** 可信 ingress接受用户文本或附件
- **THEN** acceptance/Turn owner构造 canonical append并由统一 ContextStore owner原子提交 user root；CSM不创建 source revision或 tracking registration

#### Scenario: 提交模型和工具协议事实

- **WHEN** 模型产生 assistant/reasoning/tool call或工具产生与 `tool_call_id`配对的 result
- **THEN** model stream/tool execution domain构造 canonical append并由同一 owner保持 item identity、origin Turn、协议配对和 ordinal；CSM不按文本去重或接管其生命周期

#### Scenario: 工具执行期间产生的 source 不插入调用与结果之间

- **WHEN**assistant声明一个或多个tool call后，在全部匹配terminal tool result提交前，`skill_load`、team或其它producer提交了pending source/runtime事实
- **THEN**owner先收敛同一causal execution的完整tool-call/result group，未配对时不得seal或dispatch；下一plan先选择assistant tool-call group及其全部result，再选择期间pending source，不能按物理item sequence、offset或提交时间把user-role source插入其中

#### Scenario: 提交 source lifecycle 决策

- **WHEN** CSM观察到一个 instruction、tracked file或 runtime source需要新增 base、delta、恢复 item或 control state
- **THEN** CSM只返回结构化lifecycle decision。若发生在model-call preparation中，统一owner在同一transaction提交source item、applied revision/checkpoint state和assembly selection；若异步team/inbox/runtime producer在没有model call时到达，只提交幂等pending observation/ambient item及wakeup，保持applied/injected revision和diff基准不变，直到下一次真正preparation选择并seal

#### Scenario: 扩展数据不能伪装成 source 控制字段

- **WHEN** source observation或contribution 的合法 namespaced `extensions` envelope 的 `value` 中携带 `selection_only`、`replaceable_source`、`source_ordinal`、tracking state或role同名值，但 typed 控制字段不变
- **THEN** CSM、registry和seal结果不变；缺失必需 typed 字段时直接 schema error，不能用扩展或旧 metadata 填补

#### Scenario: pending source 在 seal 前重试

- **WHEN**异步producer已经提交pending ambient item，但execution尚未取得active slot、preparation失败或进程重启
- **THEN**owner按producer/source identity复用同一pending事实，不追加重复item；在首个成功sealed assembly前不得把它标记为applied/injected，也不得要求cold thread为此materialize runtime

#### Scenario: mutation transaction 失败

- **WHEN**一次model-call preparation中的source reconciliation、ToolSet switch、plan或seal任一步失败，或独立active-view rebuild事务失败
- **THEN**preparation不得部分推进新item、source committed revision、applied ToolSet revision/applied prefix epoch或消费pending transition；独立rebuild事务失败则旧view不变。若rewind/compaction view和pending transition已在更早事务成功提交，它们保持有效但不可dispatch，后续重试同一transition，不能回滚成旧view或制造半assembly

### Requirement: ToolSet 变化必须在 model-call safe boundary hard rebase

系统 SHALL 将 ToolSetSnapshot/ToolSetRef保持为独立于 canonical message和 CSM source的Provider可见权威事实。Provider `tools` SHALL仅包含按capability profile选出的少量直接工具和始终存在、名称/schema/description固定的`invoke_extension_tool(tool_name, arguments)`；其描述不得枚举内部target。直接工具或信封自身的Provider可见名称、schema、description、visibility、确认policy发生有效变化 SHALL产生 desired ToolSet revision；owner MUST在安全边界通过hard rebase生效。内层ExtensionToolCatalog的MCP/内置/自定义target增删改、schema、目录revision或执行时权限变化不属于ToolSetRef，不得仅因此创建prefix epoch。hard rebase MUST封存新的 ToolSetSnapshot/Ref、创建 `epoch_reason=toolset_changed`的新 prefix epoch并重编译 root context、active canonical/source projection和 tools；不得向旧 context追加“工具已切换”之类的软通知，也不得修改 in-flight sealed assembly。

ToolSelectionStore/ToolService SHALL只拥有 workspace/agent级 desired selection及其控制面 revision；直接工具选择影响Provider ToolSet，扩展目标选择只影响ExtensionToolCatalog。每次新 preparation 的完整 desired ToolSetSnapshot MUST由该 SessionThread ContextStore owner冻结，并与当前合法执行链的 sealed assembly binding 比较。该 binding MUST是 applied ToolSetRef/revision 的唯一权威；工具正文、policy 和 wire tool order MUST共同参与 revision/compatibility 校验。ToolSetSnapshot 作为 typed prepare/seal 输入，不另建 SwitchToolSetIntent 或独立 SessionControl observed/applied 字段。applied 与 ExtensionCatalogBindingRef及其原始model-call binding MUST在同一成功 seal 边界保存；失败 preparation/seal 不得提前推进 applied。恢复或重放历史 assembly MUST使用两种已封存binding，不得用当前 desired selection反推历史工具集或扩展目标。

#### Scenario: model call 期间修改工具选择

- **WHEN** 一个绑定 ToolSet A的 sealed model call仍在 flight时控制面把 desired ToolSet改为 B
- **THEN** 当前请求继续只使用 A，控制面记录 B的 desired selection/revision，并在下一个 model-call safe boundary之前不 seal使用 B的请求

#### Scenario: safe boundary 应用新 ToolSet

- **WHEN** 当前没有未收敛的 model/tool协议步骤且 desired revision不同于 applied revision
- **THEN** owner冻结最终完整 desired ToolSet snapshot/ref，复用既有编译器创建 `toolset_changed` prefix epoch并重编译 root/messages/tools，经 CSM reconciliation后在同一成功 seal 事务保存唯一 applied binding；不得在 seal 前由另一 owner推进 applied

#### Scenario: 切换时存在 outstanding tool call

- **WHEN** 旧 assembly已经产生尚未获得 terminal result的 tool call
- **THEN** 系统先按旧 ToolSet让已开始调用完成或返回真实失败；尚未执行且权限已撤销的调用返回绑定原 `tool_call_id`的真实 policy-denied result，全部配对收敛后才 hard rebase，且不得伪造执行成功或无配对取消

#### Scenario: safe boundary 前多次切换

- **WHEN** applied revision之后、下一次 safe boundary之前连续产生多个 desired ToolSet revision
- **THEN** 系统可以只为最终 desired revision创建下一 sealed snapshot，但必须保留控制面 revision因果审计，且不得覆盖任何已 applied/sealed ToolSet历史

#### Scenario: ToolSet 未发生有效变化

- **WHEN** desired ToolSet的工具正文、policy与原始wire tool order均等于当前 applied binding
- **THEN** 系统保持当前 prefix epoch并继续普通 canonical/source尾部追加，不创建空 hard rebase

#### Scenario: hard rebase 与 compaction 的边界不同

- **WHEN** ToolSet变化且没有显式 compaction
- **THEN** hard rebase保留相同 active canonical history和 item identity，只更新 epoch、条件化 root projection与 ToolSet binding；compaction才可以改变 active history view并产生或选择 compaction summary

#### Scenario: 同一 Turn 跨多个 ToolSet epoch

- **WHEN** 一个长 Turn在两个 model call之间应用新的 ToolSet revision
- **THEN** 后一个 model call使用新的 prefix epoch，系统不得把 prefix epoch绑定为 Turn级唯一属性

#### Scenario: Provider 无法投影旧工具协议事实

- **WHEN** 新 ToolSet下的 Provider projector无法合法表达 active history中的旧 tool call/result
- **THEN** 系统明确阻止 hard rebase并报告兼容性错误，只允许调用者选择适用的显式 compaction或终止 execution，不删除、改写或伪造旧协议事实

### Requirement: Source 必须拥有稳定 revision 与有序 lineage

系统 SHALL 为每个参与模型上下文的动态 source 分配稳定的 source identity、source kind、source revision 和内容完整性标识。首次生效的完整内容形成不可变 base/full item；后续变化形成带 from revision、to revision、diff/content hash 和稳定幂等身份的有序 delta。未生效或未追踪 source 的文件变化不得创建 delta。

#### Scenario: 首次使用 source

- **WHEN** 一个 AGENTS、已加载 Skill、团队上下文或其它动态 source 首次进入模型上下文
- **THEN** 系统保存完整 revision、hash、来源、item identity 和 assembly 关联，且不生成无来源的普通 user message

#### Scenario: 已提交 source 连续变化

- **WHEN** source 的 A revision 已提交，B revision 已进入一次 sealed assembly，之后文件变为 C
- **THEN** 系统保留 `A(base) -> A→B(delta) -> B→C(delta)` 的不可变顺序、revision chain 和幂等 identity

#### Scenario: 多个未提交变化合并

- **WHEN** active view 最新已提交可见 revision 为 A，而 B 和 C 都在下一次 sealed assembly 前被观察到
- **THEN** 系统只提交一个 A→C delta，并可在内部 provenance 中保留 B observation，但不得先创建再删除 B 的上下文 item

#### Scenario: 未加载或已停止追踪的 Skill 变化

- **WHEN** Skill activation 从未加载，使用 snapshot 加载，或已执行 untrack，随后 `SKILL.md` 发生变化
- **THEN** 系统不得因该变化创建 activation delta、自动恢复 item 或 Provider 注入

### Requirement: tracked 来源必须具备更新接线并区分观察与提交结果

系统 SHALL为持续更新的来源登记可核验的owner、版本发布入口和允许的激活边界；要求tracked的入口 MUST检查这些更新能力已绑定，并在状态或能力不符时明确失败。一次性内容使用snapshot语义。接线校验不得强制每个来源拥有独立watcher或callback；文件、配置、内存和计算结果可以由owner采用各自能力发布。CSM只消费来源版本并管理增量与追踪，不能接管变化发现或所有上下文生命周期；普通canonical用户、模型及工具协议事实不适用来源tracking，仍经同一ContextStore/Saver提交。

观察回执 SHALL明确区分未变化、恢复已提交基准、来源未跟踪和接受新观察，并独立表达是否存在待提交更新。接受观察不等于queued，更不等于injected或seal成功；来源回到已提交基准时 MUST撤销尚未seal的净增量，不追加空更新。请求操作、当前tracking状态和来源绑定封存时的状态 MUST分别表达；canonical引用的不适用与来源引用的未跟踪 MUST由类型合同区分，不能共用含义不明的状态值。封存时状态不可随之后track/untrack回写。

请求字段 SHALL使用`requested_mode`，当前控制字段保留`tracking_status`，封存引用使用`source_tracking_at_seal`；外部`skill_load.mode`与派生`tracked`不变。canonical引用的封存追踪字段 MUST为`None`，来源引用 MUST明确为`tracked|untracked`。字段变更由itemized唯一codec按整份封存版本处理，新封存明确登记版本；旧封存读取、重试与fork重编码 MUST保留原字节和历史身份，不允许混合版本字段、公开别名或双写。

#### Scenario: 缺少更新入口不能宣称tracked可用

- **WHEN** 持续更新的instruction来源请求登记为tracked，却没有有效owner版本发布接线
- **THEN** 要求tracked的入口明确失败并指出缺失能力，不以tracked标记或手动observe能够产生diff作为更新已可用的证据

#### Scenario: 接受观察与回到基准分别表达

- **WHEN** 已提交基准为A，观察到B后尚未seal又观察到A
- **THEN** B观察回执只表达接受和pending，第二次回执表达基准恢复并取消pending；没有空delta、重复item或已提交revision推进

#### Scenario: 未跟踪与未变化不可混同

- **WHEN** 一个来源已untrack，另一个tracked来源在观察中保持相同版本
- **THEN** 两者回执分别表达未跟踪与未变化；要求tracked的生产入口遇到前者报错，不能统一解释为无需处理的False

#### Scenario: 请求模式与封存状态独立

- **WHEN** 两个来源item以不同模式封存到同一消息，随后某来源执行untrack，旁边还有canonical工具结果
- **THEN** 来源当前状态与封存时状态分别可读且历史不被改写，canonical引用表达tracking不适用，不被归类为未跟踪来源

### Requirement: 指令正文必须由来源owner统一发布有效版本

系统 SHALL为Agent基础提示词R01保留唯一权威默认入口，显式配置覆盖默认值；实现不得同时维护内置配置和代码中的不同默认提示词或悄悄兜底。R02 MUST将可覆盖说明与软件生成的运行时身份事实分开，再由owner统一发布有效正文和revision；移除具体项目示例，可覆盖说明不得篡改权威事实，配置首选/fallback与本次实际模型调用身份必须区分。

系统 SHALL支持可选的外部提示词文件作为明确选择的正文输入，由来源owner按有效配置完成读取、覆盖解析及版本发布；默认仍使用内置inline正文。有效配置 MUST确定唯一正文输入，文件作为替换正文而非隐式拼接第二份指令，具体配置键不在此固定。显式文件不存在、不可读或解析失败 MUST报错，不退回inline或空正文；文件有效正文变化须推进相应revision，经既有激活边界进入上下文。来源owner负责后台读取，CSM及model-call路径只消费已发布冻结版本。

R01/R02持续更新 SHALL复用现有配置reload和runtime generation发布链路，核清各自支持的边界并补齐缺口，不另造平行热更新流程。验收 MUST从真实配置或正文输入变化开始，经过owner发布和真实激活入口证明新版到达模型，并证明旧sealed前缀不变；只手动调用observe生成diff不构成接线验收。

#### Scenario: R01配置变更经过真实更新链路

- **WHEN** 用户修改真实Agent提示词配置并完成配置reload，下一允许的execution或model-call激活边界到达
- **THEN** owner发布的有效正文与revision被实际请求消费，CSM明确新旧版本关系且旧sealed前缀不变；不将generation重建当作任意call内立即热更新

#### Scenario: R02说明覆盖不改变身份事实

- **WHEN** 用户覆盖运行时说明，配置中还存在首选和fallback模型
- **THEN** 有效正文保留软件生成的权威身份事实，不把项目示例或配置候选列表冒充本次实际调用身份；对应有效内容变化通过owner版本发布

#### Scenario: 文件正文更新和读取失败

- **WHEN** 已显式选择的提示词文件正文变化，或该文件随后不可读
- **THEN** 前者经owner发布新正文revision并在配置边界激活；后者显式报告失败并遵循来源readiness，不悄悄改用inline，工具和模型请求准备均不临时读取文件

### Requirement: 扩展目录、MCP指引和信封执行必须使用同一生效快照

系统 SHALL以固定的`invoke_extension_tool(tool_name, arguments)`作为全部非直接工具的唯一Provider信封，不保留`invoke_custom_tool`兼容别名。ExtensionToolCatalog SHALL把内置扩展、自定义及MCP target规范化为稳定identity、schema hash、目录revision和可审计权限策略；Provider ToolSetRef不得包含这些target的清单。McpCatalogOwner SHALL在启动、MCP `tools/list_changed`、重连和配置candidate切换时完整分页读取并验证`tools/list`，按语义变化发布不可变目录snapshot与独立channel通知；不具备变化通知的server只在显式刷新/重连或配置的有界轮询后承诺刷新，不得在model-call preparation执行网络请求。McpToolGuidanceProducer SHALL只从已验证目录派生有界、确定性的名称/描述/参数指引与删除tombstone，并明确注册为`root_placement=tail_only`的CSM source；原始MCP prompt/resource/instructions不得被动提升为指令。

Workspace配置`context.resource_activation.overrides.mcp_tool_catalog` SHALL使用既有`turn|model_call`策略且默认为`turn`。目录binding与对应指引revision SHALL在同一边界一起冻结并进入sealed assembly；后续变化只在下一允许的边界以user-role增量生效，不得硬改前缀或令模型看到与dispatcher绑定不一致的目标。每个调用 SHALL用产生它的model-call已封存`ExtensionCatalogBindingRef`解析精确target，记录target/schema revision、调用与结果provenance；执行点重新执行最新权限校验，撤权/缺失返回原`tool_call_id`的明确terminal失败。配置candidate或连接刷新失败不得半发布目录或指引，已在flight的调用持有旧generation lease并按真实结果收敛。Provider可见信封始终存在，即使目录为空。

同名target解析 MUST以稳定server identity与规范公共名称为键；跨server或内置扩展的公共名称冲突必须在candidate发布前显式拒绝或按已声明的确定性命名空间区分，不能依赖列表顺序/最近连接覆盖。工具指引只描述已验证schema所需的最小调用形式，外部description作为不可信数据经长度、转义和注入隔离后成为`tail_only`内容；其文本不得更改授权、工具schema、source根资格或Provider信封description。目录条目缺少可执行schema、分页不完整或协议revision冲突时不发布半目录。

#### Scenario: MCP目录变化但Provider工具形状不变

- **WHEN** MCP server发出`tools/list_changed`且完整relist验证出target新增、修改或删除，而当前Turn冻结为`turn`
- **THEN** 已sealed与本Turn后续model call仍使用旧catalog和指引；下一Turn同时取得新catalog binding与user-role指引delta，ToolSetRef、prefix epoch及既有wire bytes不变

#### Scenario: model_call边界及时生效

- **WHEN** 冻结policy把`mcp_tool_catalog`设为`model_call`，新validated目录在两个model call间发布
- **THEN** 下一安全preparation在全部outstanding tool result配对后一起冻结新目录和指引，按协议顺序追加一个合并delta；不会请求期访问MCP server或触发ToolSet hard rebase

#### Scenario: 扩展调用已封存后目录更新或撤权

- **WHEN** 旧model call发出了`invoke_extension_tool`调用，执行前目录generation或权限变化
- **THEN** 系统不把该调用改投新同名target；旧generation lease维持原identity并按最新授权返回真实结果或与原tool_call_id配对的明确拒绝，结果记录内层target/schema revision，Provider信封schema不变

#### Scenario: 空目录仍保持稳定信封

- **WHEN** 当前没有任何extension target，之后MCP server发布首个工具
- **THEN** 两个model call的Provider ToolSetRef都包含相同的`invoke_extension_tool`定义；前者调用未知target显式失败，后者在相应activation边界获得新目录/指引，不借信封增删制造epoch

### Requirement: Root system 与 post-user source role 必须按位置确定

每个source owner SHALL用typed `root_placement=root_eligible|tail_only`声明根指令资格，CSM不得从路径、文本或`extensions`推断。每个合法prefix epoch的最顶层root context SHALL至多投影为一个`wire_role=system` source item。首次assembly及实际compaction、rewind、Provider ToolSet hard rebase或fork目标首次assembly时，compiler MAY把仍在active view且被owner声明`root_eligible`的受信来源当前完整状态按确定顺序合并成该新root；`tail_only`来源，包括默认外部MCP工具指引，只能作为user-role数据。除此之外，第一条真实用户消息之后任何source完整内容、delta或恢复 SHALL作为独立逻辑item追加，按本spec的来源消息分组合同投影为`wire_role=user`消息，不得改写、合并或前插旧前缀。新epoch物化必须记录base/delta lineage并避免重复选择，旧canonical item和sealed assembly保持字节不变。wire role不能改变canonical source identity、scope或Turn归属；多个逻辑source item允许共享一条新wire消息，不改变其独立生命周期。

#### Scenario: 首次组装 root context

- **WHEN** Session/branch 在第一条真实用户输入前组装基础说明、初始 AGENTS 和 Skill metadata
- **THEN** 系统将这些初始贡献编译为一个最顶层 system wire item，并封存其 source provenance

#### Scenario: 用户消息后加载完整 Skill

- **WHEN** 模型在真实用户消息之后调用 `skill_load` 并读取完整 Skill activation
- **THEN** activation 作为新的 ambient source item追加到上下文尾部并投影为 user role，不前插或合并进 root system item

#### Scenario: rewind 或 compaction 完整恢复

- **WHEN** tracked source 在 rewind 后需要恢复当前完整 revision，或在 compaction 后物化为完整 revision
- **THEN** 只在实际新epoch重建时将`root_eligible`有效完整状态合并到新root；`tail_only`完整恢复仍是user-role来源item，消息按来源分组合同聚合或独立发送，旧base/delta只保留lineage且不重复投影；若无实际重建，全部恢复只能尾部追加user-role来源

#### Scenario: Provider 原生工具 role

- **WHEN** Provider 协议要求 tool call 或 tool result 使用其原生 role/item type
- **THEN** 系统保留该协议 role；post-user user-role 规则仅约束 CSM source item

#### Scenario: ToolSet hard rebase 重编译条件化 root

- **WHEN** ToolSet变化使一个仅在特定工具 policy下启用的初始 instruction source改变 included状态
- **THEN** assembly compiler只在新的 `toolset_changed` epoch按owner的`root_placement`重编译最顶层root system item；`tail_only`和真实用户/工具协议事实仍保留其原有顺序与role，旧sealed assembly不变

### Requirement: CSM 来源内容必须统一封装并向模型解释更新语义

系统 SHALL 对首次激活、后续增量、完整替换和恢复使用统一的模型可见来源块，system root 与 user 来源更新沿用相同的来源表达规则。每块 MUST提供不泄露私有locator的稳定来源标识、名称、类型、操作、目标revision以及适用的基准revision，区分完整正文与unified diff，并表达相应追踪模式和原有信任资格。封装 MUST明确正文边界，正文中的类似标签不得被解析为其它来源的包头。来源定位元数据使用资源 URI，不透传内部 endpoint 或凭据；展示、正文与诊断中的原生路径按统一寻址 owner 的字段职责合同处理，不成为第二定位关系。

系统 SHALL 提供集中且版本化的CSM模型使用提示，说明这些消息来自应用上下文管理器、与真实用户任务的区别、来源与版本的对应关系，以及unified diff上下文行、删除行和新增行的含义。提示 MUST说明新版替代同来源旧版、删除行对应的旧规则不再生效、完整替换的适用范围，以及untrack只停止未来更新而不撤销正文。跨来源显式切换 MUST说明旧、新来源的替代关系，不能把它解释为对其它来源的普遍撤销。包装、提示和wire role MUST NOT提升来源的信任资格或改变既有指令优先级。该提示自身 MUST经过已登记instruction来源和同一seal链路，不得由Provider adapter临时拼入。

新来源协议 SHALL使用显式`boxteam-context-sources:v2`及frame标记`source_protocol=2`，以`replaces`列出被替代的`source_identity`与`revision`。同来源更新 MUST列出精确基准；跨来源替代仅允许显式切换同名Skill。软件 MUST按有序封存内容核对可见目标的名称与类型，不能替代canonical用户或工具事实。历史v1来源块 MUST按原协议解析、重编码和重放，不能补写替代字段或改变原sealed bytes；新旧帧可在同一继承前缀中共存。

#### Scenario: 首次正文与后续差分使用同一表达规则

- **WHEN** 来源A先以完整revision A1进入root或tail，之后以A1→A2增量更新
- **THEN** 两次投影均携带可关联的稳定来源标识、操作和版本信息；后者明确标注diff及基准，模型提示解释增删行和A2对A1的替代关系，不能只投影裸正文或裸diff

#### Scenario: 完整恢复不会撤销其它来源

- **WHEN** 来源A通过完整载荷恢复或刷新，来源B保持有效
- **THEN** 来源块标明A的目标revision和完整操作，集中提示限定替代范围为A；B的内容和信任资格不变，旧sealed消息保留供历史复现

#### Scenario: 不可信来源与正文中的标签

- **WHEN** tail_only来源正文含类似封装的标签或试图把自身声明成system指令
- **THEN** 软件封装边界和受信来源元数据保持明确，该正文仍属于原来源；其声明不改变root资格、其它来源标识或canonical用户任务身份

#### Scenario: 同名Skill切换明确旧来源版本

- **WHEN** 已封存Gateway来源的Skill A1，后续显式加载同名Workspace来源B1
- **THEN** 新块以replace操作及具名`replaces`引用A1的来源身份与版本；冷恢复保留该范围，A1原封存字节不变，其它名称来源不受影响

#### Scenario: 旧来源协议续写与fork保持原形

- **WHEN** 历史前缀含v1来源块，当前版本继续执行并产生v2来源块，再fork与冷恢复
- **THEN** 原v1块、request hash与历史owner保持不变，新增块使用v2，恢复不依赖当前文件或把旧块重编码成v2

### Requirement: CSM 来源消息默认聚合但保留逐来源投影配置

系统 SHALL 在seal之前按确定性顺序，将同一model-call preparation中可连续追加的CSM来源批次默认聚合为一条`user`消息；真实用户输入、assistant/tool协议事实或其它既定因果边界之间的来源不得跨界聚合。Workspace上下文配置 MUST允许切换为逐来源独立`user`消息，两种方式共用相同来源块、item选择、生命周期与提交链路，只改变新消息分组。配置 MUST遵循现有Workspace覆盖和最终schema校验规则，未知取值明确报错；新增配置归入功能域及子系统，不另造并行配置入口。

逻辑item SHALL继续作为管理、选择和封存追踪单位。聚合消息 MUST保留各item的引用、顺序、来源绑定、revision和封存时追踪状态，允许同一消息同时含tracked和snapshot来源，不能形成一份消息级tracking状态。分组结果 MUST随assembly封存；配置切换只作用于后续尚未封存批次，不得重新合并、拆分或格式化旧消息。Provider adapter MUST只消费已封存分组，重试和历史恢复复用原结果。

配置键 SHALL为`context.source_messages.grouping`，仅接受`aggregate`与`separate`，默认为`aggregate`。新封存的Provider frame SHALL以非空有序`item_refs`保存全部成员，并使用seal binding格式版本4；只有已封装的user来源frame允许多成员，canonical及system frame保持单引用。历史版本1、2、3的单引用格式 MUST仅由同一codec读取并按原版本精确重编码，运行态统一使用有序引用。此编码调整不改变SQLite schema版本；历史frame bytes、request hash及owner身份不变。

#### Scenario: 多来源与混合追踪状态默认聚合

- **WHEN** 同一安全追加批次包含tracked来源A的增量和snapshot来源B的完整激活
- **THEN** 模型收到一条含两个独立来源块的user消息；每个逻辑item仍可单独选择、审计和关联提交，A与B各自的追踪状态不因共享消息而变化

#### Scenario: 显式选择逐来源消息

- **WHEN** Workspace将来源消息分组配置为逐来源独立消息
- **THEN** 同一批次的A与B分别投影为user消息，来源顺序、封装、版本语义及生命周期与聚合模式一致，不能额外生成来源revision或prefix epoch

#### Scenario: 切换配置与重试不改写历史

- **WHEN** 聚合批次已seal，之后切换为逐来源消息并重试旧assembly或准备新批次
- **THEN** 旧assembly与父前缀逐字节不变，重试沿用旧分组；仅新批次采用新配置，adapter不得自行重新分组

#### Scenario: 聚合不跨协议事实或已封存边界

- **WHEN** 两个来源批次之间存在真实用户输入、tool协议事实或先前已seal的来源消息
- **THEN** 各批次分别在其合法位置投影；全部tool result仍先于工具执行期间产生的来源，不跨边界凑成一条消息

### Requirement: 已知资源变化必须通过代码内装配的观察、快照和语义激活链路

系统 SHALL 对代码内明确装配的文件、Gateway受认证内部快照及显式登记的网络Skill提供统一的变化观察（原「权威内存来源」适配已随 `remove-agent-memory` 移除 `MemoryStateAdapter`/`MemoryStateReader`/`memory_states` 装配点，不在当前装配面内）、稳定快照、解析与语义发布流程；typed适配合同可以供测试注入替身，但本change不得要求可安装资源插件、plugin manifest监视或动态执行任意provider/loader/reaction。已登记观察来源和已发布语义资源 MUST是不同身份与存储边界：`source_id`标识owner scope内的软件登记来源，`resource_id`标识可由一个或多个来源及其它语义资源派生的语义资源；路径、watch key、display URI均不得充当这两种身份。每个底层文件监视订阅 MUST 按monitor instance、规范化私有locator及recursive/filter/exclude/correlation/options等完整语义共享并引用计数，不能误合并不同观察合同，也不得为每个SessionThread建立独立watcher。资源观察、Job事件、配置生命周期和context source通知 MUST使用相互隔离的事件channel、queue、cursor、背压和故障域；资源事件不得进入job专属队列。

观察事件只能把已登记来源标记为dirty/change/gap/overflow，不能携带正文、credential或直接成为来源/语义revision。SourceReconciler MUST 用已知来源适配的稳定一致性合同形成不可变 `ObservedSourceRevision`：文件用允许根、稳定双读与完整原始 byte hash，其它来源用其自身可验证版本 token，不能把后者伪装成文件读取（文件SourceReconciler只处理file；网络Skill由其owner获取并校验后接入同一发布链路，不伪装成file；Gateway global Skill使用精确typed raw owner/Workspace source，以已接受byte revision组合publication revision，不恢复generic gateway_snapshot、version_token或memory-state）。ResourceDerivationGraph MUST从已接受的来源/语义依赖revision按无环拓扑、版本化loader、语义diff和CAS发布不可变`ResourceSnapshot`，并保留可验证source lineage；依赖环、缺失required依赖和跨generation不一致的多输入组合 MUST显式失败。同一来源可派生多个资源，多个来源可合成一个资源；原始来源变化但某facet语义payload不变时该facet不得推进revision或追加context item。重复、乱序、rename burst、丢失或overflow MUST通过有界reconcile收敛；失败时保留上一份valid snapshot并显式标记unavailable/diagnostic，required consumer不得使用旧valid替代当前失败。启动、Gateway内部快照重连和已知来源登记变更 MUST先完成已登记边界的initial reconcile并通过readiness gate；不得递归扫描未知目录、运行周期`rg`或根据物理目录猜测资源owner。

配置改变已知来源登记时，config domain owner MUST先在独立shadow lifetime scope校验候选配置与受影响的订阅、完成initial reconcile与health检查，再原子发布新配置/来源generation、阻断旧generation继续发布并排空旧scope；失败时只关闭candidate scope，旧generation保持权威，新旧generation不得同时发布同一来源。动态配置只能更改已知source registration和业务policy，不得安装可执行provider/loader/reaction。进程内释放机制不得决定candidate有效性、发布时机或业务reload policy。事件变化不得主动唤醒所有thread、直接追加context item或修改sealed assembly。

每个owner进程 MUST由代码内固定、不可被动态配置移除的最小`ResourcePlatformBootstrap`启动本进程事件服务、process-root lifetime scope、内置file snapshot能力，并只登记本配置域的发行内置配置、用户覆盖与允许的Workspace覆盖这些已知精确locator。Gateway bootstrap不得读取Workspace `.boxteam/workspace.jsonc`，Workspace bootstrap不得接管Gateway控制面配置。bootstrap不得注册模型source、执行业务reaction或取得ContextStore writer；动态配置只能替换其下已知来源登记与业务policy。新candidate配置必须先完成schema校验、受影响来源的initial reconcile和health再发布；已有valid generation时失败保留旧generation并显式报告，冷启动不存在valid内置/合并配置时必须直接失败，不得以空图或旧字段兼容配置继续。

#### Scenario: 文件变化在没有模型请求时完成资源发布

- **WHEN** 已登记的AGENTS或SKILL文件发生变化且当前没有模型请求
- **THEN** 共享file monitor只发布轻量变化事件，reconciler稳定读取并发布新的ResourceSnapshot；系统不唤醒各SessionThread、不追加context item，也不等待下一次before-model才读盘

#### Scenario: 观察事件溢出后有界恢复

- **WHEN** resource observation channel发生overflow、gap、事件乱序或provider重连
- **THEN** 系统把受影响的已登记descriptor标为dirty，执行有界reconcile并在恢复readiness前阻断依赖required资源的新dispatch；不得扫描未登记workspace或把event sequence当resource revision

#### Scenario: 同一底层资源被多个消费者使用

- **WHEN** config、SkillCatalog、AGENTS或UI file event消费者订阅同一个规范化watch key
- **THEN** runtime只维护一个共享provider subscription并按consumer引用计数分发，任一consumer退出不应中断其它consumer，且每个channel保持独立背压

#### Scenario: 一个Skill来源派生两个独立语义资源

- **WHEN** 已登记`SKILL.md`只修改description、只修改正文，或只修改当前忽略的frontmatter字段
- **THEN**来源raw revision可以推进，但metadata与activation分别只在自身模型可见payload变化时推进语义revision；第三种变化不推进任一facet或注入item，catalog entry仍可绑定当前来源lineage，不能把metadata和正文误用一个resource revision

#### Scenario: 多层配置合成且不同watch语义不得共用

- **WHEN** 有效Workspace配置由inline、用户、local与workspace覆盖层合成，其中两个consumer对同一locator使用不同recursive/filter/correlation选项
- **THEN** config owner按既有层级/schema/merge合同产出一个有效配置语义revision，资源平台记录完整依赖向量；不同watch选项保持独立订阅，配置变化只按配置reload policy发布业务candidate，不直接修改任何已seal assembly

#### Scenario: 派生依赖缺失或混合代际

- **WHEN** 一个AGENTS链或有效配置的required来源缺失、解析失败、依赖成环，或candidate generation把新旧来源混合成一个快照
- **THEN** 目标语义资源明确unavailable并保持旧valid可审计，required model dispatch等待有界恢复或失败；不得发布混合revision、空默认值、直接请求期读盘或改写已提交前缀

#### Scenario: 配置来源登记原子替换

- **WHEN** 配置在运行中增加、移除或替换已知来源登记与订阅，而内置adapter/loader/reaction代码保持不变
- **THEN** 新配置/来源generation在shadow状态完成初始快照和健康检查后一次切换，旧generation随后排空；失败时继续使用旧generation并返回明确诊断，不产生双重revision或动态装载代码

#### Scenario: 配置监视不依赖可变配置自举

- **WHEN** 动态配置尚未解析、被重新配置或candidate配置验证失败
- **THEN** 固定bootstrap仍能监视已知配置精确locator并报告candidate失败；动态配置不能移除bootstrap，失败candidate不能让旧valid generation与核心事件诊断一起消失

#### Scenario: MCP工具目录只派生受控工具指引来源

- **WHEN** 已配置MCP Server提供工具或其工具目录发生变化
- **THEN** 明确的McpCatalogOwner验证完整工具目录，McpToolGuidanceProducer从其派生一个`tail_only` CSM指引来源；原始MCP prompts/resources/server instructions不自动注册或注入，亦不安装可执行资源插件；未来消费MCP resources/subscriptions须另行定义来源owner与版本合同

#### Scenario: delegated child 冷恢复保留创建 Gateway 来源

- **WHEN** 真实委派调用已发布child creation record、初次execution尚未调度时进程退出，随后从权威catalog与record冷启动恢复
- **THEN** 创建调用Trace身份冻结于`task_seed.gateway_id`并参与creation preimage；`FrozenThreadSeed`只从该record映射它，初次dispatch在调用`start_job`前显式绑定该身份，结果reply在调用`RuntimeSourceDispatcher.submit`前绑定同一身份，二者均由既有Job/wakeup持久合同继续传递并在作用域结束后恢复Context；缺字段或非法值明确失败，直接Workspace调用必须显式保存`null`，不能从启动进程或当前请求补值、不能回退本机路径

#### Scenario: 工具目录独立于 Skill 来源可用性

- **WHEN** 经真实Gateway调用`GET /api/v1/tools`，包括Gateway Skill来源处于unavailable状态
- **THEN** 无durable thread owner的合成工具面只产出固定tool schema，仍返回`skill_load`的同一tool id与`name/mode`输入schema及请求`request_id`；不得因此构建或读取Skill catalog，真实执行的来源readiness与冻结激活义务保持不变

#### Scenario: 首次绑定只等待 Gateway 尚未监听的有界窗口

- **WHEN** 冷启动恢复的独立执行根首次绑定Gateway Skill来源，而Gateway仍在等待受管Workspace健康、尚未监听
- **THEN** 仅`ConnectTimeout`或cause/context链中`OSError.errno == ECONNREFUSED`允许重试；请求与退避共用现有连接配置的一个总墙钟预算（默认30秒），每轮GET前检查剩余预算，连接后延迟响应同样不能越过deadline；超时明确记录错误并发布unavailable且不建立观察任务，不接受迟到快照。DNS、网络不可达、TLS、无底层拒绝原因及其它传输错误、401/403、坏协议、source-unavailable都只尝试一次；后续观察不重试，首次失败后的新ensure入口可以重新取得该配置预算，不增加生产常量或隐式回退

### Requirement: 来源获取与更新触发必须可配置组合且复用同一快照链路

系统 SHALL 将owner私有获取地址解析、快照获取、更新触发、对账发布和上下文激活分开配置与执行。外部URL或其它私有locator只用于owner获取内容，模型可见寻址沿用统一资源寻址owner合同，不新增公开URI语法或把获取地址当作resource identity。系统 MUST支持通过配置引用代码内已登记的获取和触发能力，不得把来源限定为本地、Gateway、HTTP三类或仅按scheme固定分派策略；新协议需要先登记实现，未知协议、未登记能力和不支持的组合明确报错，不得剥离scheme改读本地路径或静默切换策略。配置不得执行任意代码、安装插件或维护第二套快照、tracking状态。


配置来源 SHALL通过代码登记表选择 typed fetch 配置、runtime factory 和可选事件订阅端口；配置 roundtrip MUST保留已登记类型的全部字段，获取地址解析仅在所选实现中执行。生产当前只登记 HTTP，新增协议 MUST同步登记实现与配置 schema；不要求所有协议提供事件能力。获取端口只返回原始 bytes 或未变化标记及私有条件缓存，accepted 正文、validator、revision 和 generation MUST由同一 owner 在完整校验成功后原子接受；无 accepted 基准的未变化结果必须拒绝。已有 v1 身份中的 `:http:` 与 `http_<id>` SHALL保持不变且视为 opaque，MUST NOT据此选择获取协议，亦不得重绑既有 tracked 历史。

触发 SHALL支持event、poll和manual；默认选择一种，允许显式组合事件与低频周期对账。poll间隔 MUST可配置为分钟、小时等正的有限周期，错误类型、非正值和不支持的参数组合在最终配置校验时明确失败。所有触发 MUST汇入同一owner的对账入口并合并重复请求，使用同一快照获取、校验和发布链路。本地文件监听、Gateway发布通知和HTTP周期检查只是可覆盖的默认组合；选用event但来源缺少所需事件能力时 MUST明确报错，不自动回退到poll。

Gateway本地来源默认由Gateway观察并发布，Workspace默认接收发布通知后及时安排后台获取和校验；Workspace获知版本的方式 MUST允许配置切换为轮询同一认证快照接口，Gateway本地如何观察与Workspace如何获取更新分别配置。Gateway MUST先发布可获取的快照再发轻量通知，通知只携带来源身份、revision等线索，Workspace仅订阅其实际消费的来源或目录。首次订阅、重连和事件gap MUST主动对账，覆盖初始获取与建立订阅之间的变化窗口；重复、连续或乱序通知最终收敛到当前可验证快照，不要求逐个注入中间revision。

显式登记的网络Skill SHALL由所属Workspace或Gateway在后台获取和更新。Workspace私有来源由Workspace负责，Gateway共享来源由Gateway统一检查上游并向Workspace分发快照，不由各Workspace重复请求同一共享链接。HTTP条件请求可使用ETag或Last-Modified减少传输；这些validator不得直接替代语义内容revision，相同模型可见payload不重复发布语义更新，缺少validator时仍须比较取得的正文。网络获取失败、快照认证或正文校验失败 MUST显式标记来源不可用并遵循既有readiness合同，不能以旧内容冒充成功更新。获取内容与激活指令分别遵循来源资格和显式加载规则。

更新触发策略 SHALL与turn/model_call激活边界、snapshot/tracked加载模式分别配置。任何观察方式都不得在模型请求准备或skill_load路径临时读盘、联网，不得直接修改sealed item、主动唤醒所有thread或因检测到更新自动执行Skill。新增配置 MUST归属相应Gateway或Workspace配置域，遵循既有合并、schema与来源generation切换合同；不增加跨域读取。

#### Scenario: 同一来源切换事件与轮询

- **WHEN** 用户把Gateway来源的Workspace更新方式从event切换为poll并配置检查间隔
- **THEN** Workspace改为周期检查同一认证快照接口，source/resource identity和已追踪绑定不变；Gateway可以继续文件观察和发布，不保留两套同步状态或隐式双开策略

#### Scenario: 新获取协议复用已有触发能力

- **WHEN** 软件已登记某特殊获取协议和poll能力，用户为显式来源组合两者
- **THEN** owner通过所选能力进入相同对账和快照链路，不要求它伪装成本地文件或HTTP；若协议或能力未登记，则配置明确失败


#### Scenario: 已登记事件能力覆盖激活与配置回滚窗口

- **WHEN** 来源已登记事件订阅，候选初始获取后、配置激活前发生变化，或旧代次休眠期间收到触发后配置结算失败
- **THEN** 来源先订阅后取初始快照，并在激活或回滚恢复时通过同一 observer 对账；休眠代次不获取或发布，成功结算后排空退役任务和订阅。event_poll 的周期超时不得取消在途事件等待

#### Scenario: 事件订阅意外退出必须显式不可用

- **WHEN** 已激活来源的事件等待失败或观察任务意外退出
- **THEN** 记录原始异常并发布 unavailable，保留旧正文但不得报告健康或隐式改用 poll；候选准备失败仍保留原代次与 validator

#### Scenario: Gateway本地观察策略与Workspace获取策略独立切换
- **GIVEN** Gateway本地Skill由原文件owner发布，Workspace经认证快照与发布通知消费
- **WHEN** 管理者修改Gateway的`resources.skill_sources.local_updates`，选择event、poll、manual或显式event_poll
- **THEN** 配置沿原候选prepare/apply/health/promotion/rollback链路热切换并排空旧订阅；分钟／小时周期经最终schema校验，错误组合明确失败，Workspace的gateway_updates策略不被改写
- **AND** 旧代次迟到事件及错误不得覆盖当前发布；候选准备失败保留旧观察和accepted快照，最终提交失败恢复原观察代次
- **AND** manual仅在初始获取或明确刷新时对账；本机管理刷新入口`POST /api/gateway/skill-sources/refresh`复用原读取／校验／发布链路和本地管理授权，联邦只读授权不获得刷新能力

#### Scenario: Gateway先发布再通知

- **WHEN** Gateway检测到已订阅Skill来源或目录发生有效变化
- **THEN** 新快照先可通过认证接口取得，随后发布身份和revision通知；相关Workspace及时获取并校验后更新accepted快照，正文不靠通知承载

#### Scenario: 首次订阅与重连不遗漏更新

- **WHEN** 来源在初始快照获取和订阅建立之间变化，或订阅重连、收到gap
- **THEN** Workspace主动对账到当前已发布版本，重复或旧通知不使accepted快照回退；无需持续高频轮询或重放每个中间版本

Gateway 共享与 Workspace 私有网络来源 SHALL在各自配置域通过 `resources.skill_sources.sources` 显式配置稳定id、逻辑name、层内priority、HTTP获取参数与独立更新策略；数组整体替换，id在owner内唯一。HTTP获取当前仅支持poll/manual，默认poll/1h；没有已登记事件协议时event/event_poll MUST明确拒绝。获取地址MUST保持owner私有，稳定identity和display URI MUST按owner及配置id产生，不随正文、获取URL或目录优先级变化。200正文MUST有界且为严格UTF-8；304 MUST对应带validator的条件请求、存在accepted正文且响应正文为空。validator变化本身不得制造内容revision。候选失败MUST保留原generation及accepted事实，旧代次的迟到正文与错误不得发布；普通导航只读取已接受快照。

#### Scenario: Workspace 私有来源复用配置 shadow 与原获取 owner
- **WHEN** 管理者配置私有 HTTP Skill、重载无关配置或明确刷新该工作区来源
- **THEN** Workspace 复用共享来源相同的获取／校验实现，但独占其来源身份和 accepted 快照；无关重载 MUST复用当前代次且不触发 manual 获取，实际来源变更先准备候选、提交后排空旧代次，失败不污染旧正文、validator 或发布版本
- **AND** `POST /api/v1/resources/skill-sources/refresh` SHALL遵守现有工作区管理授权和精确目标路由，返回无获取地址及正文的 workspace_id、publication revision 与原始来源可用性；publication revision 使用 `sha256:jcs:v1:` 合同，原始正文 revision 仍为原字节 `sha256:`
- **AND** 原始来源进入同一个 network 层目录／facet／CSM，优先级变更 MUST NOT自动改绑现有 tracked 来源；显式 skill_load 可按新目录切换并冻结，进程重启 MUST按封存正文恢复历史，不按当前 URL 重建

#### Scenario: 网络来源按owner获取且validator不充当内容版本

- **WHEN** 两个Workspace消费同一Gateway共享HTTP Skill，或者该链接的ETag变化但正文语义不变
- **THEN** Gateway统一按配置获取上游并分发快照，Workspace不重复直连；validator变化本身不形成activation delta，无validator时仍能按正文判断更新

#### Scenario: 事件能力缺失与更新失败显式可见

- **WHEN** 配置event却没有相应事件能力，或网络获取、认证、正文校验失败
- **THEN** 前者明确拒绝配置且不悄悄轮询，后者发布明确诊断并按readiness阻断依赖required来源的新dispatch；旧sealed内容不变，不能宣称更新完成

#### Scenario: 快照更新与激活相互独立

- **WHEN** 后台poll或event已接受新Skill快照，而thread仍在turn冻结边界内，或该Skill以snapshot模式加载
- **THEN** 当前冻结请求或snapshot正文不自动变化；只有符合加载模式及后续激活边界的调用才能使用新版，获取新快照不等于执行其中指令

### Requirement: 进程内资源释放必须只有一个无业务决策的所有权机制

系统 SHALL 用唯一`LifetimeScope`合同持有和释放进程内task、可撤销订阅、client及子scope；`EventChannelService`只传递通知，语义`ResourceRegistry`只发布不可变snapshot，二者均不得成为第二个dispose owner。每个订阅/cleanup登记 MUST返回可撤销handle；scope关闭 MUST先拒绝新登记、取消并排空所拥有的子task/子scope、按逆取得顺序等待释放其它handle。同一scope的并发close只执行一次并共享结果，成功后重复close幂等；失败必须保留`close_failed`及未释放handle的诊断、后续调用重报失败，不得标记为closed或留下无人持有的异步task。业务owner决定何时关闭scope，并在释放失败时将typed状态事件投到其所属的状态channel，同时向调用方保留原始错误；事件服务不可用时仍 MUST 显式返回/抛出错误并记录诊断，不得虚报成功。`LifetimeScope`自身 MUST NOT 依赖事件服务、选择channel或发布业务状态，也 MUST NOT 决定Skill追踪、config reload、thread idle、资源activation、外部资源删除或持久状态变更。`CancellationSignal`只传递取消意图，不能替代close；Turn child关闭必须解除父信号hook。Web局部订阅继续遵守React effect cleanup，不额外建立并行的全局dispose registry。

#### Scenario: 关闭子作用域不停止共享监视

- **WHEN**一个child ThreadRuntime卸载，且config或其它consumer仍持有同一底层watch的订阅handle
- **THEN**child scope只释放自己的handle；底层watch保持运行，直到最后一个consumer handle释放，且已提交ContextStore/CSM/ResourceSnapshot不变

#### Scenario: 候选配置generation失败只回收候选

- **WHEN**新配置candidate的health失败或其已知来源订阅在shadow启动期间抛错
- **THEN**composition owner关闭并排空candidate scope，旧active scope、Registry revision和bootstrap仍有效；scope本身不把失败candidate发布为active

#### Scenario: 释放失败不得宣称完成

- **WHEN**一个child cleanup失败、异步task在关闭时仍未排空，或已关闭scope再次收到登记
- **THEN**失败与阻断原因显式报告，未关闭资源不得被标记为已释放；已关闭scope不得接受新资源或创建无人持有的回调task

#### Scenario: owner报告释放失败而scope不发布状态

- **WHEN**scope关闭返回`close_failed`，其domain/composition owner仍可使用事件服务
- **THEN**owner把typed失败状态事件投到对应`resource.state/*`或`config.lifecycle/*`状态channel并保留原始错误；`LifetimeScope`不选择channel、不发布事件，通知本身不成为持久状态事实

#### Scenario: 状态channel不可用时释放错误仍可见

- **WHEN**scope释放失败且owner投递状态事件时事件服务已不可用
- **THEN**owner显式返回/抛出原始释放错误并记录投递失败诊断，不得报告释放成功或静默吞掉错误

### Requirement: 外部业务资源的持久lease不得伪装成dispose

跨Turn terminal、browser、MCP、dev-server或Node调试进程的操作占用 SHALL由唯一持久operation lease账本记录typed资源身份、holder/operation身份与恢复状态；实际资源状态、停止、删除和恢复只由对应domain owner/provider核实并执行。Node调试必须用`(workspace_id, session_id, thread_id, debug process identity)`的typed`node_debug_process`资源身份，归属由debug owner校验而非模型参数决定。工具调用必须从typed tool/provider contract取得resource-use，不得在通用账本中按`terminal_id`、`pageId`等参数名推断资源或自动伪造登记。取消Turn和进程内scope close本身不得直接停止跨Turn业务资源；idle卸载由domain owner根据业务保留策略及所有有效lease执行释放。terminal/browser按`add-workspace-persistent-resource-management`的唯一合同立即停止、核实并删除`thread`临时资源，保留`workspace`实例且移除该Thread关联；其它资源遵循各自owner/lease/blocker合同。旧内存stopper缺失、provider不可达、核实失败或仍有其它有效lease时不得把资源标记为`stopped`。`SessionResourceProviderRegistry`只路由列表/控制，不与业务owner或lease账本并行保存第二套资源状态。

Node调试的`debug process identity` MUST是每次启动唯一`process_instance_id`，而非PID、Inspector端口或单次tool/Web operation identity。debug owner须在spawn前durably登记绑定精确thread/lifecycle generation/launch preimage与一次性nonce的`launch_pending` claim，并在spawn后由nonce、OS进程起始身份和Inspector握手证明同一实例，才能登记PID/端口和运行态；该owner-held claim跨Turn持续，不因启动调用的短期`execution|debug_control` lease结束而消失。启动中崩溃只允许凭该claim/nonce核实原实例：证实不存在才结清，证实仍运行才接管或按domain策略停止，不可证明则保持`reconcile_required`及residency/delete blocker，不能认领/停止仅复用了PID或端口的其它进程。替换/重启必须先核实并结清旧实例，再建新实例claim；通用账本只保存身份、状态和证据引用，不负责搜索/杀进程或决定停止策略。

#### Scenario: 重启后缺少资源控制能力

- **WHEN**持久lease恢复后内存stopper不存在，且调用方要求停止外部资源
- **THEN**系统向实际owner查询并取得核实过的停止结果，或返回明确不可用/`reconcile_required`；不得只更新账本就向用户报告`stopped`

#### Scenario: 一个操作结束但其它lease仍有效

- **WHEN**同一外部资源被两个有效operation lease占用，其中一个Turn取消
- **THEN**只收敛该Turn的lease；除非资源owner在其业务协议下明确验证可停止，否则不能停止资源或自动释放另一holder的lease

#### Scenario: 活动Node调试进程的lease阻止闲置卸载

- **WHEN** debug owner有未结清的`launch_pending`进程claim、核实某thread进程处于`starting|running|paused|stopping`，或backend重启后无法确认旧进程是否已停止
- **THEN** 唯一外部资源lease账本保留该thread的占用/`reconcile_required`恢复事实，residency owner按 `add-itemized-rollout-context` 的唯一策略保留脱敏 blocker，不宣称cold；只有debug owner核实`idle|exited|failed`并结清lease后才可解除阻断，通用账本与`LifetimeScope`都不决定停止策略

#### Scenario: Node启动窗口崩溃与PID复用

- **WHEN** `launch_pending` claim提交后，backend在spawn前、spawn后登记进程属性前或停止确认前崩溃，重启时原PID/端口可能已复用
- **THEN** claim仍以原process_instance_id/nonce归属原thread，实际debug owner按OS起始身份和Inspector握手核实后才接管或停止；无法核实则保留`reconcile_required`与idle/delete blocker，不把PID/端口相同的其它进程标为原资源，也不由一次tool/Web操作lease终结推断资源已停止

### Requirement: 统一资源寻址 必须隐藏locator并固定资源来源

**归属声明（消除两套定义并存）**：本 requirement 涉及的资源 URI 语法、scope/scope_id、gateway authority、kind 闭集、星型解析顺序与拒绝码，一律以 `add-unified-virtual-resource-addressing` 的具名 requirements 为唯一 owner 与唯一实现：语法引用「URI 语法必须保留固定段序并单一实现」，scope 引用「scope 必须取自定稿闭集且 scope_id 对所有 scope 必填」，kind 引用「kind 闭集定稿且描述符闭集独立不可混用」，错误引用其集中登记。scope 明确采用该 owner 的 `workspace`/`user`/`gateway`/`inline` 闭集，`memory` 不是 URI scope；本 change 不另维护取值表，也不自行发明取值或解析器；普通 files 导航不产生语义来源描述符。本 requirement 只声明管理资源的隐藏 locator、四个标识分离、activation snapshot 恢复、skill_load name-only 与安全 display URI 合同。

系统 SHALL为可寻址且可向模型或客户端展示的管理资源提供规范`boxteam://`虚拟资源URI。workspace AGENTS以及本地、网络、inline Skill条目 MUST分别使用可区分的逻辑scope/kind/name（`memory` 已确证不是 URI scope，MUST NOT 作为 scope 出现）；URI中不得出现绝对路径、真实网络endpoint、credential、provider私有handle或正文。内部语义聚合的必填 nullable display_uri 只引用统一寻址owner requirement「内部语义聚合资源不得伪造 URI」，不得用catalog身份伪造可访问地址。每个资源 MUST区分模型可见`display_uri`、内部稳定`source_id`、语义`resource_id`和provider私有`provider_locator`：URI只是locator/provenance，不是任一资源身份、授权凭据、dedupe key或业务幂等键，revision/hash/snapshot reference不得编码进URI。

URI resolver MUST在当前principal、workspace/gateway binding、resource activation snapshot和requested operation下返回typed resource handle，并重新校验resource capability；来源的`source_id`和provider私有locator只能从Registry-owned绑定解析，不能由URI、资源名或模型输入推导；不得把URI percent-decode后直接拼接为文件路径。parser MUST只接受统一owner登记的grammar，并按其结构/身份段与文件child段各自规则处理；只有文件段严格decode一次，身份段不解码。非法编码、userinfo、credential、query/fragment、控制字符、非法分隔符与越界scope按同一owner拒绝。旧context item、history或sealed assembly MUST使用当时封存的resource id/revision/hash/snapshot ref恢复，不能根据display URI重新读取当前资源。

外部获取地址 SHALL只由来源owner用于获取；网络层是Skill名称选择层级，不新增`network` URI scope。网络Skill的模型可见display URI MUST按其owner绑定沿用同一寻址合同，不以外部URL替代，也不把来源获取协议混入公开URI解析器。

`skill_load` MUST继续只接受`name + mode`。软件从当前冻结的SkillCatalogSnapshot解析exact resource handle，工具结果和context provenance可以返回安全`display_uri`，但该字段属于ResourceProvenance而不是Skill metadata；通用`read_file`不得通过虚拟URI或`.boxteam/.../SKILL.md`承担Skill activation/tracking。其它工具只有显式声明typed operation/capability时才可接受相应虚拟URI，不得提供绕过loader、Registry或CSM的万能资源读写入口。

#### Scenario: 模型理解Skill来源但看不到位置

- **WHEN** 模型按名称加载一个Gateway全局Skill
- **THEN** 结果和后续context provenance可以显示`boxteam://gateway/{gateway_id}/resources/skills/{skill_name}/SKILL.md`，但承担资源定位职责的参数、provenance 和引用字段不以`${BOXTEAM_HOME}`物理路径或 provider handle 代替 URI；普通正文／展示／诊断按统一寻址的字段职责保留原生路径，credential 继续保护

#### Scenario: 伪造或越界虚拟URI

- **WHEN** 调用方提交含`..`、双重编码、userinfo、未知fragment、错误workspace/gateway scope或不具备requested capability的URI
- **THEN** resolver在访问provider前显式拒绝，不读取文件、网络或内存资源，也不产生context、history或审计之外的业务副作用

#### Scenario: 同名覆盖不改绑tracked资源

- **WHEN** tracked registration绑定Gateway Skill的resource id后，workspace出现同名高优先级Skill及不同display URI
- **THEN** 新catalog snapshot可以让未来名称解析指向workspace资源，但既有registration继续绑定原resource id；只有显式重新`skill_load`才能按rebind合同切换

#### Scenario: 历史URI不重解当前正文

- **WHEN** 物理文件移动、provider locator变化或同一display URI当前revision已更新，随后恢复旧sealed assembly
- **THEN** 系统使用assembly封存的resource id/revision/hash/snapshot ref得到原字节；required snapshot缺失则明确失败，不根据当前URI或当前文件补造历史

### Requirement: 资源上下文激活边界必须可配置且确定

系统 SHALL只支持`turn`和`model_call`两种resource context activation boundary，默认值 MUST为`turn`，并允许按resource kind覆盖。queued execution取得active execution slot时 MUST取得一个`ResourceActivationPolicySnapshot`及有序`ExecutionResourceSnapshot`；policy snapshot保存revision/hash及完整规范化`activation_policy={default_boundary, overrides}`，包括尚未选中resource kind的override；恢复校验同一policy hash/revision，不从bindings反推或读取当前配置，按唯一冻结合同在当前 execution及同用户Turn的retry/resume内不可变，配置热更新只影响后续首次取得冻结事实的新用户Turn或独立internal execution。`ExecutionResourceSnapshot`固定全部turn-bound binding；若存在model-call-bound kind，每次tool protocol收敛后的安全model-call preparation MUST建立引用parent execution snapshot的`ModelCallResourceSnapshot`，逐字节复用turn-bound binding并只重新冻结model-call-bound binding。已经sealed或in-flight的call始终不可变。

真实 execution、可空 Turn、parent与默认冻结复用只引用 `add-itemized-rollout-context` 的「Context plan 必须绑定精确资源激活快照和安全来源」合同。配置名 `turn` 也适用于无 Turn 的 internal execution：以其真实 active execution为冻结边界，禁止创建或借用用户 Turn。该唯一合同约束 user Turn 的 retry/resume 与 internal 恢复，本 change 不维护第二份身份模型。

effective boundary MUST记录在每个resource binding中；系统不得以一个assembly级`boundary=turn|model_call`单值表示可能混合的资源。`skill_load` MUST使用产生该tool invocation的Execution/ModelCall snapshot解析Skill名称、entry与revision，不得在工具真正执行时改用更新的catalog snapshot。

ResourceActivationCoordinator MUST只通过唯一Saver/ContextStore port提交typed snapshot；SQLite/JSONL/detail持久化、assembly binding、hash和恢复由itemized rollout owner实现。CSM、资源观察流水线或MCP适配不得建立第二snapshot writer/catalog。

两种边界都只能读取ResourceRegistry已经发布的内存snapshot/revision，不得在model request路径执行stat、read、目录枚举、网络fetch或`rg`扫描。若required resource仍处于dirty/gap/reconciling/unavailable状态，系统 MUST等待有界reconcile或明确阻断dispatch，不能旁路Registry。系统不得提供`immediate`、TTL、request-count或注入次数型activation；config runtime reload policy与context resource activation boundary必须是两个独立配置维度。

该配置 MUST位于Workspace配置域的`context.resource_activation.default_boundary`和`context.resource_activation.overrides`，按既有Workspace配置层级递归合并并对最终结果执行schema校验。默认配置 MUST显式提供`default_boundary="turn"`；override key只能是已登记resource kind，value只能是`turn|model_call`，未列kind继承默认值。开发模板、schema和配置诊断 MUST与默认结构一致；系统不得为不存在的旧字段增加alias、deprecated兼容分支或第二套内部模型。

#### Scenario: 默认Turn内保持资源一致

- **WHEN** Turn取得active slot并冻结revision A，随后文件监视发布revision B且该Turn继续第二次model call
- **THEN** 默认`turn`配置下第二次call仍使用A，B只在下一个Turn生效，两个call都不重新读盘

#### Scenario: model_call边界应用已发布变化

- **WHEN** tracked Skill配置为`model_call`，第一次call后Registry已经完成A到B的reconcile，且tool协议已经收敛
- **THEN** 下一次model-call preparation冻结B并按CSM/stable-prefix合同追加delta；正在执行或已sealed的第一次call不变化，preparation不读取源文件

#### Scenario: 同一 assembly 混合两种边界

- **WHEN** 最终policy配置`agent_spec=turn`且`tracked_skill_activation=model_call`，同一Turn执行第二次model call
- **THEN** 新ModelCallResourceSnapshot引用parent execution snapshot，逐字节复用agent spec binding并只更新tracked Skill binding；每个binding分别标记effective boundary，assembly不得用单值boundary覆盖二者

#### Scenario: activation policy 热更新不改变当前 Turn

- **WHEN** 当前Turn冻结的policy为`tracked_skill_activation=turn`，其tool-loop期间配置热更新为`model_call`
- **THEN** 当前Turn所有call继续按原policy复用Turn binding；新policy revision只由下一个取得active slot的Turn冻结，不重组当前assembly、不改变stable prefix

#### Scenario: skill_load 使用发起工具调用的快照

- **WHEN** 模型在catalog revision C1下发出`skill_load(name)`，工具执行前Registry发布同名Skill revision C2
- **THEN** 工具仍按发起该tool call的ResourceActivationSnapshot解析C1及其exact resource revision；C2只能按后续activation boundary生效，不得造成模型所见名称与实际加载来源漂移

#### Scenario: queued Turn按active slot时刻取快照

- **WHEN** 一个Turn已accept但尚在thread FIFO排队，资源在其取得active slot前发布新revision
- **THEN** 该Turn首次snapshot使用取得active slot时的最新required revision，而不是acceptance时刻或前一Turn的revision

#### Scenario: required资源尚未完成reconcile

- **WHEN** activation boundary到达时required resource处于gap、dirty或unavailable且有界reconcile未成功
- **THEN** 系统返回明确resource readiness错误并保持旧assembly/context前缀不变，不使用过期默认值或执行请求期直接读取

#### Scenario: Workspace覆盖单个资源类别

- **WHEN** 最终合并的Workspace配置把`tracked_skill_activation`设为`model_call`而其它kind未覆盖
- **THEN** 只有tracked Skill在每次安全model call重新冻结snapshot，其它资源继续继承默认`turn`；未知kind、未知boundary或错误类型使配置加载明确失败

### Requirement: SkillCatalog 必须隐藏路径并分离 metadata 与 activation

系统 SHALL 从Workspace本地、Gateway本地、显式登记的网络来源和内置inline来源构造确定性的有效catalog，并只向模型暴露唯一Skill名称及描述。名称选择优先级 MUST为 `Workspace 本地 > Gateway 本地 > 网络来源 > 内置 inline`。同层来源允许显式配置优先级，未区分时按同优先级处理；同层同优先级仍有相同规范化名称 MUST明确报冲突，不按扫描或响应先后选择。网络Skill由所属Workspace或Gateway获取和更新，经来源与内容校验成功后进入SkillCatalog，统一支持`skill_load(name)`；获取owner及落盘缓存不得改变其网络层归属。Skill 目录 metadata 与 `skill_load` 输入只通过名称表达选择，不以绝对路径、相对路径、外部获取地址或 catalog locator 定位；普通文件读取及正文、诊断继续按统一寻址 owner 的字段职责合同处理。一个 `SKILL.md` 的metadata与activation SHALL共享同一个已登记`source_id`，但拥有不同语义`resource_id`/facet identity并分别生效：安全frontmatter parser禁止custom tag/alias/object构造，name/description必须是唯一、有界scalar；其它字段不进入metadata/activation payload、facet revision或策略。activation只使用frontmatter结束offset之后的精确正文bytes。工具name只可作为已构造catalog map的受校验精确key，绝不得参与路径拼接。

SkillCatalog MUST由ResourceRegistry发布版本化immutable catalog snapshot/revision。inline使用发布manifest；Gateway本地和Workspace文件来源只监视并reconcile各自固定`skills/<entry>/SKILL.md`一层边界，拒绝越界symlink/未知递归且不运行`rg`；网络来源只消费owner已校验的快照，不在目录解析或skill_load时请求外部地址。Gateway只返回带安全display URI、不含私有locator的descriptor，不写Session状态；资源变化异步更新catalog desired snapshot但不主动唤醒thread或广播context item，实际metadata delta只在该thread配置的activation boundary形成。内容更新 MUST保持原source/resource identity，以revision表达变化；目录层级或层内优先级变化只改变后续冻结catalog的名称选择。

activation registration MUST绑定`name + catalog_entry_identity + resolved_source_identity`。snapshot/tracked自动逻辑不得因同名优先级变化改绑来源；原tracked source不可读返回`tracked-source-unavailable`并阻止未记录dispatch。显式再次`skill_load(name, tracked)`时，相同entry复用registration并按需刷新，不同effective entry则由同一owner原子冻结旧registration、接受当前完整activation候选并建立新tracking，返回安全`source_rebound=true`且不删除旧item。显式snapshot同样按当前调用的冻结catalog选择entry，但成功后 MUST停止该名称已有tracking，不因切换来源遗留旧registration。同source/revision已在active view可见时复用正文、不追加重影，仍须完成请求mode的状态切换并在回执表达实际tracking状态；不同revision的显式snapshot可追加新immutable activation。控制状态与激活候选共同接受，applied/injected仍只随assembly seal推进；失败保持原有效状态。tool invocation retry必须先按调用幂等identity恢复原结果。

由于`tracked-source-unavailable`会阻止模型dispatch，系统 MUST提供受信Session控制API，以精确`session_id + thread_id + normalized skill name + mode=untrack`调用和模型工具相同的CSM mutation。该API不得接受或返回路径、不得读取source、删除既有item、绕过权限/幂等/checkpoint版本化或维护第二套tracking状态；它不是新的Skill卸载工具。模型工具与控制API对`not_tracked`、`tracking-state-conflict`和成功untrack MUST返回同一安全状态合同，Web不得只改本地状态。

#### Scenario: Gateway 全局 Skill 可用于所有工作区

- **WHEN** `${BOXTEAM_HOME}/skills/<skill>/SKILL.md` 提供有效名称和描述且 workspace 没有同名覆盖
- **THEN** 该 Skill 出现在所有工作区的有效 catalog 中，但具体 Session activation 和 CSM 状态仍由工作区 Saver/ContextStore 持久化

#### Scenario: 同名 Skill 按固定优先级解析

- **WHEN** Workspace本地、Gateway本地、网络来源或inline层存在同名Skill
- **THEN** 系统按 `Workspace 本地 > Gateway 本地 > 网络来源 > inline` 选择唯一有效entry，并保持内部origin provenance；网络来源归属不随获取它的owner改变，模型看不到私有地址

#### Scenario: 网络Skill校验后按名称加载

- **WHEN** Workspace私有HTTP Skill或Gateway共享网络Skill完成来源、metadata与正文校验并发布目录快照
- **THEN** Skill以网络层entry进入名称解析，metadata可按激活边界进入上下文，模型能够使用`skill_load(name)`加载冻结正文；未经校验的内容不得进入有效目录

#### Scenario: 同层优先级与同名冲突

- **WHEN** 同层两个来源提供相同规范化Skill名称
- **THEN** 显式不同优先级选择高优先级entry；同优先级或都未指定优先级时明确报冲突，不按枚举顺序选择，也不悄悄降到其它层掩盖冲突

#### Scenario: 更新内容和调整目录优先级

- **WHEN** 网络Skill正文更新，随后层内优先级变化使同名其它entry成为目录首选
- **THEN** 内容更新只推进原来源revision，source/resource identity稳定；已tracked registration继续追踪原来源，新的名称选择只由后续显式加载按其冻结catalog采用

#### Scenario: tracked 期间出现同名高优先级 Skill

- **WHEN**thread已tracked Gateway global Skill，随后workspace创建同名Skill并由ResourceRegistry发布新的effective catalog entry
- **THEN**metadata可追加catalog delta，但既有tracking仍绑定原source且不自动切换；显式tracked加载按冻结catalog切换并建立新tracking，显式snapshot加载则切换为固定版本并停止旧tracking，旧activation item保持不变

#### Scenario: tracked source 不可读时仍可人工停止追踪

- **WHEN**activation boundary因原tracked ResourceSnapshot被删除、失去权限或不可用而返回`tracked-source-unavailable`，模型已无法运行工具
- **THEN**获授权用户可经Session控制API按thread和logical name执行同一untrack mutation；操作不读取路径/source、不删除旧item，成功后下一次model request不再检查该registration

#### Scenario: catalog 变化由共享监视与有界reconcile发布

- **WHEN**固定Skill根内的直接entry新增、修改或删除，期间没有模型请求
- **THEN**共享file provider标记catalog边界dirty并有界枚举固定一层、稳定读取后发布新catalog revision；系统不唤醒thread或注入，不运行递归rg、不扫描workspace其它目录、不跟随越界symlink

#### Scenario: metadata 与 activation 分别生效

- **WHEN** catalog 已向模型提供一个 Skill 的 name/description，但模型尚未调用 `skill_load`
- **THEN** metadata 可以存在于模型上下文，而 Skill activation 正文不得因此加载或生效

#### Scenario: 忽略其它 metadata 字段

- **WHEN** `SKILL.md` frontmatter 还包含 name/description 之外的字段
- **THEN** 完整文件的source snapshot hash仍可记录字节变化，但metadata和activation facet extractor不读取、解释或投影这些字段；若两个有效facet payload均未变化，则不追加context item

### Requirement: 文件 source 必须通过稳定且有界的统一读取协议

系统 SHALL 让 Skill metadata、Skill activation、AGENTS及其它文件 source由对应SnapshotProvider/SourceReconciler统一通过`StableSourceReader`读取，再由ResourceDerivationGraph产生语义facet。reader MUST 接受provider-owned `SourceReadHandle`与允许根；workspace source可由workspace provider读取，Gateway global Skill MUST由Gateway provider读取并通过仅内部可用、受认证且以身份关联的`StableSourceSnapshot`向workspace发布entry/source identity、来源revision hash、byte length和精确正文；Workspace据此派生metadata/activation resource identity，随后按同一来源revision派生metadata/body语义resource。handle MUST 能在进程重启后由持久catalog entry identity重新解析，该 handle、临时进程 token 与 provider 私有 locator 不透传给模型、普通工具结果或 canonical history；已授权的来源正文与诊断允许含有原生路径，不由消费者据此打开本机文件。CSM、middleware、`skill_load`和model-call preparation不得直接调用reader。 Gateway原始快照MUST使用现有本地token认证，同时绑定真实Gateway身份与本Gateway已登记的本机Workspace UUID受众；远端投影、错误身份、失败认证及错误byte hash MUST被拒绝。Workspace MUST通过进程共享typed source接收，不得回读Gateway本机路径；initial reconcile、断连不可用、gap后全量快照及重启重建归现有source lifecycle，prepare与skill_load不得网络取数。

每个读取attempt MUST 重新验证允许根containment，拒绝非普通文件和不允许的symlink，通过no-follow handle分别在读取前后比较filesystem identity、size与高精度时间签名，并进行第二次独立打开/读取；只有两次读取各自稳定、identity/signature一致且精确内容hash相同才可接受。系统最多尝试三次，持续变化返回`source-changed-during-read`并阻止candidate、seal和dispatch。超过产品固定byte上限返回`source-too-large`且不得截断；非严格UTF-8返回`source-invalid-encoding`。`source_snapshot_hash` MUST覆盖接受的完整原始字节，不得隐式规范化BOM、换行、空白或Unicode；logical source使用版本化extractor计算模型可见`facet_revision_hash`，只有facet payload变化才可追加item。AGENTS整文件facet使用原始正文；Skill metadata只使用name/description的确定性表示，activation只使用frontmatter之后的精确正文bytes；source item再使用版本化确定性serializer。

published Skill descriptor MUST绑定catalog revision、entry/resource/source identity、metadata revision/hash、display URI和Registry-owned provider binding ref。`skill_load`从当前activation snapshot取得exact published resource snapshot；若entry、catalog generation、metadata binding或snapshot availability冲突，则返回`skill-catalog-snapshot-conflict`，不得在调用路径枚举目录、重新读取文件或将旧名称静默映射到新文件。tracked registration MUST只保存resource/source identity和catalog/provider binding revision，provider handle/locator归Registry/provider私有；registration在后续activation boundary消费该resource的published snapshot，不得假设Gateway global Skill有workspace本机路径或因同名优先级变化自动改绑。

#### Scenario: 文件在读取期间持续变化

- **WHEN** writer在metadata、snapshot、tracked或AGENTS读取的两次确认之间替换或原位修改文件，且三次attempt都不能得到相同稳定字节
- **THEN** 系统返回`source-changed-during-read`，不推进observed/pending/committed revision，不追加item、不seal assembly也不dispatch模型请求

#### Scenario: Gateway global Skill 由原 provider 跟踪

- **WHEN** workspace thread tracked一个Gateway global Skill，随后Gateway或workspace backend重启，或workspace出现同名高优先级Skill
- **THEN** Resource platform通过持久entry/resource identity重建原Gateway provider并发布其snapshot，registration继续消费原resource；同名变化只影响未来catalog resolution，除非模型显式重新`skill_load`，否则不得改绑或要求workspace直接读取Gateway物理路径

#### Scenario: 大文件、非法编码和字节变化明确失败

- **WHEN** source超过固定byte上限、不是严格UTF-8，或模型可见facet只改变BOM、换行、空白或Unicode字节
- **THEN** 前两类分别显式返回`source-too-large`、`source-invalid-encoding`且不截断；后一类形成新的facet revision而不是被隐式规范化为旧revision。只改变被忽略frontmatter字段时仅source snapshot hash变化，不产生伪metadata/activation delta

### Requirement: `skill_load` 必须支持 snapshot、tracked 与 untrack

系统 SHALL 提供 `skill_load(name, mode)`，其中mode只能是`snapshot`、`tracked`或`untrack`，缺省值为`snapshot`。snapshot与tracked MUST是互斥加载模式，每次显式加载以请求mode为准，不继承先前tracking状态；untrack是仅停止追踪的操作。工具 SHALL只接受模型可见名称；snapshot/tracked由软件通过发起该调用的ResourceActivationSnapshot中的SkillCatalogSnapshot解析内部resource/source identity与published snapshot，不能取得provider-owned handle/locator；untrack则按当前thread与受校验逻辑name定位唯一active tracked registration，不重新解析当前effective entry也不读取source。每个`(session_id, thread_id, normalized_skill_name)`至多一个active tracked registration；重复active状态返回`tracking-state-conflict`。工具结果 MUST返回实际mode、tracking状态、是否追加item及安全来源/revision标识，可以包含安全display URI，不得暴露内部路径、provider locator或完整Skill正文。系统不得提供会立即删除既有Skill上下文的另一工具，也不得要求unload再load或增加重复的reload参数才能刷新。

一次snapshot/tracked加载 SHALL原子接受相应激活候选和模式控制变更；失败保留原registration、pending候选及有效正文，不产生先停止旧tracking后加载失败的半完成状态。成功的snapshot转换 MUST清理或替换未seal且与本次显式冻结版本冲突的旧tracking候选，不能在停止追踪后又自动注入该候选。成功接受不等于injected；真正提交版本仍由后续assembly seal确立。新版本生效 MUST通过不可变更新表达与旧版的替代关系，保留历史sealed字节；同版本复用正文不得跳过模式切换。

同名 Skill SHALL由唯一 thread owner 在原控制行分配显式选择次序，并分别保存已接受与已封存的选择次序；后者只能由 assembly seal 推进。当前选择 MUST NOT仅根据某来源 revision 曾经可见判定。preparation 冻结当前控制 CAS，已冻结 plan 按原 CAS 校验；目录重绑、模式变更、重启和失败重试不得绕过此边界。选择状态 MUST随 checkpoint 保存及 rewind 恢复；旧库通过具名24→25维护迁移新增字段，以原封存来源确定零次序基准，不能重编码历史。

#### Scenario: 默认 snapshot 加载

- **WHEN** 模型调用 `skill_load(name)` 或显式使用 `mode="snapshot"`
- **THEN** 系统取得本次调用冻结的已发布Skill revision并追加或复用不可变activation，成功后停止该名称已有tracking；随后不自动跟进更新，也不在rewind移除后自动恢复，调用路径不临时读取文件或网络

#### Scenario: 重复 snapshot 不产生同 revision 重影

- **WHEN**同一source revision的snapshot activation仍是当前选择并在active view，模型再次调用`skill_load(name, snapshot)`
- **THEN** 复用原安全identity、不追加第二个item，并确保该名称无active tracking；正文已存在不掩盖实际模式切换，回执表达最终snapshot与未追踪状态

#### Scenario: 一次调用刷新snapshot到新版

- **WHEN** A版snapshot仍在active view，而本次调用冻结快照已发布同来源B版，模型再次调用`skill_load(name)`
- **THEN** 一次调用接受B版activation并明确其取代A版，无需先unload或untrack；旧sealed A版不变，随后仍处于snapshot模式

#### Scenario: 复用旧版本显式切回与重启

- **WHEN** A来源原版本已经封存，显式切换并封存同名B来源，随后再次选择A的相同原版本，在接受后、seal前重启runtime
- **THEN** owner保留尚未封存的A选择，使用原execution冻结正文恢复并追加具名替代B的更新；seal后重复加载A不再追加，A和B原sealed bytes及历史owner不变

#### Scenario: 连续选择及epoch重建不复活旧来源

- **WHEN** 一次seal前连续选择同名A、B、A，或已封存A→B→A后进行compaction／rewind
- **THEN** pending只保留最后接受的选择；新epoch只携带目标checkpoint所选来源的最后完整状态及后续diff，被取代来源不复活，旧assembly和请求hash保持原样

#### Scenario: tracked显式切换为snapshot

- **WHEN** Skill已tracked，模型省略mode或显式以snapshot加载，冻结版本可能与已可见版本相同
- **THEN** 成功后该名称的追踪停止，正文按版本追加或复用；之后源文件更新不再自动注入，也不自动恢复被移出的正文

#### Scenario: snapshot切换为tracked不重复正文

- **WHEN** 同来源同版本snapshot已可见，模型显式调用`skill_load(name, mode="tracked")`
- **THEN** 系统复用正文并建立checkpoint-versioned tracking，回执表达tracked状态，后续允许的激活边界消费新版

#### Scenario: snapshot转换不遗留旧tracking候选

- **WHEN** tracked来源有尚未seal的自动更新候选，随后显式snapshot成功加载本次调用冻结的版本
- **THEN** 待提交正文与本次snapshot对齐且tracking停止，不再注入与它冲突的旧自动候选；若加载失败则原候选和tracking保持不变

#### Scenario: 刷新或来源切换失败保持原状态

- **WHEN** snapshot/tracked刷新或显式来源切换在候选校验或owner事务提交时失败
- **THEN** 原tracking、pending及当前有效正文保持不变，不遗留半完成的停止或改绑；同一次tool invocation重试沿用原幂等结果合同，不重新选择其它版本

#### Scenario: tracked 加载并检测变化

- **WHEN** 模型调用 `skill_load(name, mode="tracked")`
- **THEN** 系统追加或复用当前完整activation，保存checkpoint-versioned tracking registration，并在配置的turn或model_call activation boundary比较该resource的published revision/hash

#### Scenario: tracked 文件没有变化

- **WHEN** activation snapshot中的tracked resource revision/hash等于active view最新已提交可见revision
- **THEN** 系统不追加 activation item、delta 或重复 selection

#### Scenario: tracked 文件发生变化

- **WHEN** activation snapshot中的tracked resource revision不同于active view最新已提交可见revision
- **THEN** 系统计算到当前 revision 的一个 delta，以 user role追加到上下文尾部，并在 sealed assembly 提交后才推进 committed diff 基准

#### Scenario: untrack 停止后续追踪

- **WHEN** 模型调用 `skill_load(name, mode="untrack")` 且该 Skill 当前处于 tracked 状态
- **THEN** 系统按thread/name命中唯一active registration并将其版本化为untracked/frozen，该registration不再消费Registry revision或自动恢复，同时不删除、改写、重排或追加任何Skill source正文；共享monitor可为其它consumer继续观察原resource，即使当前catalog已有同名高优先级entry也不得误操作新entry或遗留旧tracking

#### Scenario: untrack 不依赖当前 catalog entry

- **WHEN**thread已tracked Gateway global Skill，随后workspace出现同名覆盖，模型在尚未rebind前调用`skill_load(name, mode="untrack")`
- **THEN**系统不读取当前workspace Skill，而是冻结原Gateway registration；之后before-model不再读取原provider，当前覆盖也不会自动变成tracked

#### Scenario: Skill 操作不改变同名 instruction

- **WHEN** 当前 thread 同时登记同名 Skill 与 instruction，或仅有该名称的 instruction
- **THEN** snapshot／tracked／untrack 只定位 Skill 来源；同名 instruction 的登记、版本和追踪保持不变，仅有 instruction 时返回 skill-not-found。名称索引与冷恢复不得让不同 source_kind 互相覆盖或误报重复 Skill 追踪，已封存字节保持不变

#### Scenario: untrack 非 tracked Skill

- **WHEN** 模型对 snapshot 或不存在的 tracked registration 调用 `mode="untrack"`
- **THEN** 系统返回确定性的 `not_tracked` 结果，不伪造状态变化或 source item

#### Scenario: untrack 后重新 tracked

- **WHEN** 一个 untracked Skill 后续再次以 `mode="tracked"` 加载
- **THEN** 系统从 active view 最新可见 revision 恢复追踪；若没有可见 revision，则从冻结activation snapshot追加published完整 revision，不读取source

### Requirement: Canonical source state 与真实用户 Turn 必须分离

需要跨 checkpoint 保留的 runtime source SHALL 以无 `turn_id` 的 ambient/pending runtime item 或等价 source lineage 持久化；只在 assembly 中使用的受保护 detail SHALL 通过同一 source manifest 引用。`wire_role=user`、LangChain `HumanMessage` 或 Provider user item 不能使 runtime source 成为 canonical `user_input`、Turn root/member 或真实历史用户消息。

#### Scenario: 持久化 runtime delta

- **WHEN** AGENTS、团队角色状态或其它已生效 source 在 checkpoint 后发生变化
- **THEN** 系统追加可审计的 ambient/pending source item，并关联旧 revision、新 revision、execution 和 assembly，且不创建 user root

#### Scenario: history 读取 user-role source

- **WHEN** history API 读取一个 Provider 中投影为 user role 的 CSM source item
- **THEN** history 只返回策略允许的 source provenance/安全摘要，不把它列为用户消息或新 Turn

### Requirement: Sealed assembly 内同一 source revision 只能选择一次

系统 SHALL 以 source reference、source revision、history view revision、prefix epoch 和 assembly identity确定一次注入选择。一个 sealed assembly 中同一逻辑 source revision 至多有一个 included body；重复 prepare、seal、tool retry 或 projector retry必须复用相同选择和字节。系统不得维护跨 view 的可变注入次数。

#### Scenario: 同一 assembly 重复准备

- **WHEN** 相同 Session、history view、source revisions 和 model attempt 重复执行 context prepare/seal
- **THEN** 系统返回相同 assembly selection、source provenance、item identity 和 serialized frames，不重复登记正文

#### Scenario: 同一 source 出现多个 producer

- **WHEN** 两个 producer 以相同 source identity 和 revision 贡献同一 source
- **THEN** 系统按稳定 identity 选择一份；若正文、revision 或 hash 不一致，则返回 source identity conflict，不按文本相似度静默去重

### Requirement: Rewind 与 compaction 必须遵守模式和恢复 role

系统 SHALL在rewind时从目标checkpoint恢复版本化tracking registration，按snapshot、tracked、untrack语义重建active view，并提交`PendingPrefixEpochTransition(reason=rewind)`；compaction提交summary/view时登记对应`reason=compaction` transition。pending transition没有wire bytes且不是applied epoch。下一次真正model-call preparation需要恢复/物化tracked source时 SHALL在同一owner事务中创建完整revision恢复事实、seal首个新prefix epoch assembly并消费transition；失败保留已提交view/transition但不追加半成品、不应用epoch或dispatch。同epoch或`tail_only`恢复/物化source必须保留独立逻辑item并按来源分组合同投影为user消息；只有真实新epoch中`root_eligible`受信source的完整有效状态可合并唯一system root。

任何会用于继续执行的rewind、compaction、replay或fork目标view SHALL在创建view或pending transition前验证tool protocol closure。若anchor位于assistant tool-call group与任一匹配terminal result之间，系统 SHALL返回`tool-protocol-boundary-conflict`、保持旧view且不创建目标或transition，并只返回无正文的最近安全anchor metadata；不得自动偏移、合成result、借source item闭合协议，或把只读partial history送入sealed assembly/Provider dispatch。

#### Scenario: rewind anchor 切入 tool 调用组

- **WHEN** rewind anchor保留assistant tool-call group却排除其中任一匹配terminal result
- **THEN** 系统返回`tool-protocol-boundary-conflict`，不改变active view、不创建pending prefix epoch transition，并向调用方返回可重选的安全anchor metadata

#### Scenario: snapshot 被 rewind 移除

- **WHEN** snapshot activation item 位于 rewind cutoff 之后
- **THEN** 该 item 从 active view 消失，系统不读取文件、不重新追加也不创建 tracking registration

#### Scenario: tracked revision 被 rewind 移除

- **WHEN** rewind 目标 checkpoint 仍包含 tracked registration，但其最新已注入 revision 不在重建后的 active view
- **THEN** 下一次真正model-call preparation从activation coordinator冻结的ResourceRegistry内存snapshot取得当前published revision，并将恢复事实、首个rewind epoch assembly与transition消费原子提交；`root_eligible`可进入新root，`tail_only`按来源分组合同投影为user消息；snapshot不可用时保留rewind view/pending transition但不追加item或dispatch，且不读取文件、网络或其它provider

#### Scenario: 回到自动来源首次登记之前

- **WHEN** AGENTS 或自动 instruction 来源首次在 input checkpoint 之后的 preparation 登记，而 rewind 目标控制快照尚无该来源；自动 instruction 包括 Agent 基础说明、运行时身份、CSM 协议、Skill catalog metadata 及已启用工具的团队、Todo、Filesystem、compact 规则
- **THEN** 原 owner 仅保留该自动来源已有的 tracking 声明，清空后来观察、applied revision 与选择序号；下一次 preparation 从所属 owner 的本次冻结发布版本重新装配。已有 untracked 声明不得自动激活；Skill 正文等显式激活来源仍按目标历史边界停用，不因 root 资格、source ID 前缀或重复 register 恢复。旧 canonical item、assembly 与 request hash 保持不变

#### Scenario: untrack 状态参与 rewind

- **WHEN** rewind 目标 checkpoint 位于一次 untrack 之前或之后
- **THEN** 系统分别恢复该 checkpoint 当时的 tracked 或 untracked 状态，不使用进程当前内存状态覆盖 checkpoint 因果顺序

#### Scenario: 显式snapshot转换参与rewind

- **WHEN** Skill由tracked显式切换为snapshot，随后rewind到转换前或转换后的checkpoint
- **THEN** 系统分别恢复当时的tracked或snapshot状态；转换后的snapshot不自动恢复被移出的正文，不能因历史上曾tracked而重新启用追踪

#### Scenario: 编辑回退到模式转换前的 Turn

- **WHEN** 正常执行已产生多个 Turn 及 tracked／snapshot／untrack 转换，用户从正式编辑入口回退到指定 Turn 输入之前，包括连续回退
- **THEN** canonical 内容与 activation 引用沿来源 checkpoint 和精确 cut 恢复，追踪控制态沿该 Turn 输入前的冻结 state header 恢复；两种 checkpoint 身份由同一 owner 的 typed intent 与控制事件绑定并在同一事务校验提交，不使用最新来源 checkpoint 的后续模式覆盖历史控制态，也不改写旧 assembly 或封存追踪字段

#### Scenario: compaction 物化 tracked source

- **WHEN** active source 为 `A(base) + A→B(delta) + B→C(delta)` 且 compaction 实际重建上下文
- **THEN** compaction先提交view/pending transition，下一次真正model-call preparation把以C为完整内容的新revision、首个compaction epoch assembly与transition消费原子提交；`root_eligible`只在新root投影一次，`tail_only`保留独立逻辑item并按来源分组合同投影为post-user消息，旧链保留审计引用

#### Scenario: compaction 处理 snapshot 或 untracked 内容

- **WHEN** compaction 遇到 active view 中的 snapshot 或 untracked Skill 内容
- **THEN** 系统只能使用已提交 item/detail决定是否携带，不重新读取 `SKILL.md`，CSM 不执行自动恢复

### Requirement: 生产上下文来源必须完整登记并由正确 owner 接管

系统 SHALL 维护可验证的生产上下文来源迁移闭包，并为每类来源明确唯一 domain owner以及进入统一 ContextStore mutation owner的 intent。Agent 基础说明、运行时身份/路径、条件化团队规则、Todo/Filesystem/Skill catalog/AGENTS/压缩工具说明属于 instruction/file source（原文另有「显式启用的 memory」一项，已随 remove-agent-memory 整体移除，不再属于迁移闭包）；Goal、委派与分支结果、内部跨Session消息、团队动态事实、终端完成、模型重试控制和 checkpoint/runtime reminder属于 runtime event source。真实用户输入与附件、assistant/reasoning/tool协议事实通过 canonical append进入 owner；ToolSet通过 switch/hard-rebase进入 owner；compaction summary由 compaction owner产生。它们不得仅因参与同一模型请求而改造成 CSM source。生产请求不得包含来源闭包之外、没有 provenance的 system/user控制内容；模型工具不得把内部消息伪造为可信用户ingress。

#### Scenario: 首次组装全部初始 instruction source

- **WHEN** Session 第一次组装模型上下文，并且一个或多个条件化 instruction source 当前启用
- **THEN** 系统把每个启用来源的 identity、revision、hash、included reason 和顺序登记到同一 sealed plan，再只在该首次边界将它们编译为唯一 root system item

#### Scenario: 内部事件触发新的模型执行

- **WHEN** Goal、委派/分支回报、内部跨Session消息、团队更新、终端完成、retry 或 checkpoint reminder需要模型继续执行
- **THEN** 系统先以稳定事件 identity和幂等键提交 ambient/pending user-role source item，再通过独立 wakeup intent启动或继续 execution，不创建真实用户 acceptance、user root 或普通历史用户消息

#### Scenario: 生成 Session 的 seed prompt 明确声明语义

- **WHEN** 软件为新生成的 Session构造初始 prompt
- **THEN** producer 必须显式声明它是用户输入的可追溯派生 root还是内部 source；前者创建 canonical user root，后者走 runtime source 与 wakeup边界，系统不得根据文本内容或 wire role猜测

#### Scenario: 压缩派生请求与主上下文分离

- **WHEN** 系统为 compaction 创建派生摘要请求并将摘要结果带回主上下文
- **THEN** 派生请求拥有自己的sealed assembly和root/control items，摘要结果由compaction owner保存为canonical compaction summary并登记pending prefix transition；CSM只在下一次model-call preparation中物化需要恢复的source，并与首个新epoch assembly原子seal，不重复登记摘要正文

#### Scenario: ToolSet 与工具协议事实保持独立

- **WHEN** tool policy改变模型可见工具，或 middleware产生工具参数错误、超时、确认/拒绝等配对结果
- **THEN** 可见工具定义只通过 ToolSet switch和 hard rebase进入 sealed plan，tool call/result通过 canonical append按 Provider协议和 domain owner持久化；两者共用 ContextStore transaction但均不作为 CSM source或按文本去重

#### Scenario: 出现未登记的请求控制内容

- **WHEN** dispatch 前发现最终模型请求相对 sealed source/ToolSet selection多出或替换了未登记的 system/user控制内容
- **THEN** 系统报告具体 producer/source差异并阻止 dispatch，不通过请求期 prompt差分、合并 system message或 synthetic fallback补造 provenance

### Requirement: 模型 dispatch 只能消费 sealed assembly

系统 SHALL 删除通过 middleware观察已组装 model request并反向生成 prompt contribution、ToolSet snapshot、source provenance或 assembly的路径。canonical append收敛、outstanding tool protocol收敛、Context source reconciliation、desired/applied ToolSet比较与必要 hard rebase、plan创建和 assembly seal MUST在 framework model-call接入点之前完成。框架需要 middleware hook时，该 hook只能是无状态 dispatch bridge：消费 Saver签发且绑定本次 Session、execution/model-call attempt、assembly、prefix epoch/reason、Provider profile、prefix/frame hash和精确 ToolSetRef/policy hash的不可变 reference，读取并转发对应 sealed payload；不得维护跨请求状态、重新 prepare/seal、推断来源、合并 item或生成 fallback。框架不需要 middleware hook时，Provider dispatch SHALL直接消费同一 sealed payload。

#### Scenario: middleware bridge 转发 sealed assembly

- **WHEN** framework要求通过 model-call middleware提供最终 messages和tools
- **THEN** bridge只根据本次 sealed dispatch reference读取精确 messages、ToolSet和Provider frames，校验全部 binding后原样转发，不读取原 request形状生成任何新事实

#### Scenario: framework 不要求 middleware hook

- **WHEN** execution层可以直接把 sealed payload交给 Provider adapter
- **THEN** 系统不保留旧 projection middleware类或空兼容层，Provider adapter直接消费 Saver-owned sealed dispatch reference

#### Scenario: sealed dispatch reference 缺失或错配

- **WHEN** reference缺失，或其 Session、model-call attempt、assembly、Provider profile、ToolSet、prefix/frame hash任一项与当前 dispatch不一致
- **THEN** bridge或Provider preflight阻止请求并报告 sealed-assembly binding violation，不读取当前 LangChain request补造 assembly

#### Scenario: sealed assembly 重试

- **WHEN** 相同 sealed assembly进行 transport retry或进程恢复后的重复 dispatch
- **THEN** 无状态 bridge从同一 reference得到相同 payload bytes，不依赖上一次 middleware调用、进程缓存或可变 capture state

#### Scenario: bridge 后请求被再次修改

- **WHEN** bridge转发后仍有 middleware或adapter改变 sealed messages、tools、顺序、role、content block或serialization
- **THEN** Provider调用前的 frame/hash校验失败并阻止 dispatch；只读 telemetry不得成为修改 payload的理由

### Requirement: AGENTS、Skill 与团队状态必须使用统一 source 边界

系统 SHALL 将 AGENTS、Skill metadata/activation、团队角色/任务状态和同类动态上下文接入同一 source lifecycle。生产代码不得绕过该边界直接向模型请求追加未登记的内部 `HumanMessage`，不得让通用 read 工具承担 Skill 激活，也不得通过普通 `MessageRole.user` 创建仅用于内部状态的新 Turn。

AGENTS 是自动装配的工作区来源。回放或 rewind 到其首次登记之前时，唯一恢复 owner MUST 保留已有自动跟踪声明，清空目标边界之后的 observed/applied revision 及选择序号；下一次真实 prepare 仍从本次冻结 activation 接受正文，不回读当前文件修改历史 assembly。已有明确的 untracked 状态不能借此自动恢复；显式激活的 Skill 仍按历史边界恢复 tracking，不扩展该例外。

#### Scenario: AGENTS 文件变化

- **WHEN** 共享file provider经SourceReconciler与ResourceDerivationGraph发布AGENTS语义revision B，而SessionThread已应用revision A
- **THEN** 系统在该resource配置的turn或model_call activation boundary登记A→B delta并以user role追加；原root/base不被原地替换，model request路径不读盘也不运行`rg`

#### Scenario: 二次回放到首轮输入前

- **WHEN** 两轮真实执行后先改写第二轮，再回放至首轮输入前，目标控制快照尚无首次 prepare 登记的 AGENTS
- **THEN** 恢复保留自动跟踪声明并清空后来 revision，下一次真实 Provider 请求成功消费冻结 AGENTS 和新输入，排除被替换历史，旧封存 assembly 原字节不变

#### Scenario: 技能包文件读取与激活分离

- **WHEN** 模型通过统一寻址的技能包文件 URI 读取 SKILL.md、references 或 scripts
- **THEN** 文件 owner 按导航/读取能力返回有界内容，不隐式注册 source、activation 或 tracked state，也不写入来源控制事实；只有 name-only skill_load 按冻结 catalog/source snapshot 建立激活和追踪

#### Scenario: 文件读取后显式激活仍以冻结来源为准

- **WHEN** 模型读取 SKILL.md 后文件发生变化，再调用 skill_load
- **THEN** skill_load 仍使用发起该调用的冻结 catalog/source binding，不把普通读取结果当作激活 snapshot，不在工具路径临时重新读盘；旧 .boxteam Skill 挂载与路径白名单不恢复

#### Scenario: 团队任务状态更新

- **WHEN** 团队成员更新任务状态并需要通知协调者 thread
- **THEN** 系统写入 ambient/pending user-role source item并可启动消费 execution，不把通知伪装成真实 user root

### Requirement: 多投影与重启必须消费同一已提交事实

系统 SHALL 从同一个 Saver-owned sealed ContextRequestPlan生成 LangChain、native Provider和 history/diagnostic projection，并持久化 canonical mutation provenance、source revision、tracking state、prefix epoch/reason/manifest、desired/applied ToolSet revision、精确 ToolSet binding、selection、hash/length、reconciliation outcome和必要 detail引用。恢复旧 assembly时不得读取当前文件或当前 ToolSelectionStore猜测历史正文/工具集。

#### Scenario: 多投影顺序一致

- **WHEN** 同一 sealed assembly 同时生成 LangChain request、native Provider request 和 history diagnostic
- **THEN** 三者使用相同 selection/plan ordinal、source revision、wire role 和 append order，不按 role、created_at 或字典顺序重排或合并

#### Scenario: 重启恢复已提交 delta

- **WHEN** tracked delta 已提交但进程在下一次 model call 前重启
- **THEN** 系统从已提交 lineage 和 checkpoint state恢复 selection并验证 hash/length，不要求读取当前文件重建旧 assembly

#### Scenario: 未提交 source 变化

- **WHEN** source 文件在上一个 assembly seal 后变化，但对应 candidate 尚未 durable commit
- **THEN** 旧 assembly 和稳定前缀保持不变；下一次 reconciliation 要么提交明确的新 item，要么返回可诊断失败

#### Scenario: 当前 Provider 不支持中途 system item

- **WHEN** post-user CSM source 进入当前 Provider projector
- **THEN** 同epoch或中途新增source保留独立逻辑item并按来源分组合同投影为user消息；仅合法新epoch首个root可按owner的根指令资格重编译为system。Anthropic官方部分模型的中途system能力只保留未启用TODO

### Requirement: Context lifecycle owner 必须精确为 SessionThread

task 的 empty/last_completed_turn_fork/view_prefix_fork 初始化 SHALL 消费 `add-itemized-rollout-context` 的 checkpoint-context-branching requirement「task 上下文初始化复用统一 fork 创建流程」及「Fork item 定位与前缀捕获保持一致」；不另建 context 复制或恢复 owner。安全默认范围模式改名保留已完成 internal 与空来源行为，默认仍为 empty。view_prefix_fork 支持 item-only 的精确来源 Thread 全分支最新有效 view 定位，也支持显式 view 和 inclusive/before；在 task 调用落盘后，以一致快照或既有 source owner 保护冻结前缀并 capture。MUST 区分调用方观察 revision 与后端冻结事实，保留冻结完整性及其它操作的校验；协议不闭合或正文缺失时明确拒绝，不回退旧 view、不移动锚点。首次冻结后重试/恢复复用原 view、锚点、前缀及 capture。上下文物化在同一次 delegated child 发布前完成，member 与初始 execution intent 同事务发布；新执行的 seed/reference 在继承前缀之后注入，来源活跃执行不得转移到 child。普通公开 fork 保持 idle；其它团队 TODO 不因此解除，Goal 的实现迁移仍归 `add-workspace-persistent-tasks`。

会话目录的pending客户端投影以及已durably accepted但尚未committed的`NavigationMutationRecord` MUST NOT成为SessionThread的context/source/ToolSet/GraphBinding事实，不得创建canonical item、触发CSM reconciliation、启动owner rehydrate或改变stable prefix。导航队列入队不等于Session lifecycle准入；真正应用目录命令时必须重新通过workspace catalog/topology gate，业务producer始终依据已提交catalog和Session fence。排队的递归删除在整树catalog deleting提交前不得阻止本来合法的业务；提交后即使部分local fence尚active或客户端仍显示旧树，所有新业务准入也必须拒绝。纯导航move/rename提交后只影响导航投影，不产生context epoch、source delta或模型请求。

系统 SHALL将ContextStore、CSM registration/revision/tracking state、active view、prefix epoch、ToolSet applied binding、sealed assembly与dispatch reference绑定为同一`(session_id, thread_id)` owner。Workspace `.boxteam/navigation/session-catalog.sqlite`是Session/Folder导航关系、`sessions/YYYY/MM/DD/{session_id}` locator与不可变main_thread_id的唯一权威；Session本地`session-control.sqlite`只拥有thread catalog（唯一main row须匹配workspace pointer）、collaboration ledger/fanout及publication journal，不得成为ContextStore/CSM或canonical writer。导航移动不搬Session node、不改fork/delegation lineage或已提交context，`session.json`不再保存可变父节点/当前显示名。

`session-control.sqlite` MUST保存唯一`SessionLifecycleFence(state=active|deleting|tombstoned, lifecycle_generation, deletion_record_id?)`。新Session持久副作用的准入先取workspace `NavigationTopologyGate` shared，再取至多一个目标`SessionLifecycleGate` exclusive，读取fresh SQLite catalog active/locator并确认local fence同generation，在一个Session control事务durably建立lease/等价record后释放；已有execution/runtime lease可覆盖其内部item/source/tool callback。最终可见性publication重取topology shared→原Session gate并fresh验证catalog active及token，catalog已deleting时新可见性发布必须取消、已准入lease只可收敛。导航parent/name调整仅由workspace SQLite/topology gate负责，不写thread context；所有lock顺序为topology→至多一个Session gate→至多一个SQLite写事务，不持锁跨模型/工具/网络或整个删除排空。

`SessionOperationLease` MUST至少持久化`lease_id`、`operation_kind=thread_creation|collaboration_fanout|runtime_owner|execution|context_control|debug_control|communication_source|communication_target|federated_call|remote_observation|attachment|fork_retention|session_catalog_mutation`、稳定operation identity/preimage hash、captured lifecycle generation、holder generation/fencing token、`state=active|settling|completed|cancelled|failed`、revision和可选recovery ref；同generation/operation identity唯一且非终态可索引。专用record承担lease时 MUST以显式lease state或版本化全映射归一自己的preparing/routing/published/aborted等状态，不得按名称猜测。独立Web/API debug mutation须以`debug_control`准入，Agent工具可由其已有execution lease覆盖；该短期准入lease不替代跨Turn`node_debug_process`外部资源lease。lease不得墙钟自动到期。恢复owner验证旧holder generation失效并CAS新token后才可继续原operation或settle；fence deleting后不建新lease，旧lease只完成/取消冻结operation，不派生新root/wakeup/child。跨库主体先durable commit再terminal lease，中间崩溃按稳定identity/ref核对；删除请求settling后仍等待writer确认或幂等恢复核对，terminal后旧token callback必须失败。同一Session catalog目标条目的locator/lifecycle/归档mutation MUST以`session_catalog_mutation`竞争gate，旁路写入fail closed。

`SessionLifecycleGate` MUST按`(workspace_id, canonical_session_id)`使用workspace navigation根中的跨进程shared/exclusive OS锁；`NavigationTopologyGate`是该根中的独立跨进程短锁，所有Session准入先取shared topology。cold history/detail先在topology shared下获取fresh catalog locator和Session shared`SessionReadGuard`，释放topology后保持read guard到全部node/SQLite handle关闭并复核catalog/fence；catalog deleting前已开始reader可完成，新reader返回`session_deletion_pending`。删除owner逐Session关闭local fence前等待其read guard；锁文件不保存业务状态且不得unlink/recreate，不可验证时fail closed。

普通单child的`ThreadCreationRecord(state=preparing)` MUST在生命周期gate内、创建staging目录前提交到该数据库并承担operation lease，冻结最终/内部staging locator、artifact manifest/hash及Session lifecycle generation/catalog/collaboration precondition revision，最后重取topology shared/Session gate，并与catalog可见性在同一SQLite事务CAS验证workspace catalog active及fence仍为捕获的active generation且delegation、parent/member未漂移后推进为`published`；失败只定点清理并标记`aborted`，无预存record的目录不得被恢复路径吸收。

Session及其逻辑后代删除、`recursive=true` Folder删除统一按`add-itemized-rollout-context`的`NavigationSubtreeDeleteRecord`执行：在topology exclusive下从workspace SQLite递归冻结精确node/Session ID及每个日期locator，逐Session短时预检pinned retention，任一blocker即整批不标记删除；同一SQLite事务create-or-get幂等batch并一次CAS全树deleting。该catalog commit是所有新Session副作用准入与逻辑可见性的关闭点；旧独立`session-deletion-journal.sqlite`及先关local fence后更新JSON index的两阶段方案废弃。任一目标已漂移则整批冲突，不覆盖无关节点，不发布半棵树。

#### Scenario: pinned claim 与逻辑子树删除串行化

- **WHEN** pinned fork与包含source的Session/Folder递归删除同时尝试在各自准入边界提交
- **THEN** topology gate给出唯一顺序：claim先行则整树删除在catalog deleting前返回retention blocker、全部node仍active；删除先行则整树catalog deleting使claim零副作用失败，不能在local fence尚active时补pin

catalog整树deleting后，即使某Session local fence仍active，普通thread history/detail与新mutation也必须返回`session_deletion_pending`。删除owner按batch冻结清单逐Session取得一个Session gate、等待read guard、关闭local fence并收敛旧generation leases、child、copy attachment、communication与pin；旧writer只可按原lease和batch许可完成/取消冻结operation，不派生新root/wakeup/child。每个Session确认零非终态lease及引用释放durable后，把其日期目录定点隔离并记录进度，所有目标完成后workspace SQLite一次提交整树tombstone；崩溃保持全树deleting并只按batch manifest恢复，不能扫盘、局部开放、回退active或从已隔离目录重建CSM。

该owner identity MUST 先通过与item/storage change共享的完整canonical validator：`session_id`/`thread_id`分别具有36-byte ASCII `ses_[0-9a-f]{32}`/`thr_[0-9a-f]{32}`外形，且payload位 profile MUST 由 `migrate-identifiers-to-uuidv7` 的 `uuidv7-identifier-profile` 规定。CSM、ContextStore、Skill registration和assembly API不得接受不合该 profile 的payload、被清洗、截断、hash替代或通过旧ID path alias解析的identity；生产/测试IdentifierFactory必须复用同一validator，历史非规范ID只允许显式migration生成新的target identity和lineage。

`RolloutCheckpointRuntime` MAY 作为 workspace singleton 组装组件，但 MUST 通过显式 `ThreadRuntimeBinding` 取得唯一 thread owner port，且不得成为第二个 JSONL/SQLite/context writer。相同 source locator/hash 在不同 thread 的 tracking、latest-visible-committed 基准、delta、rewind 恢复和 untrack state 必须独立；稳定前缀不得跨 thread 拼接或复用。

#### Scenario: 同一文件由两个 thread tracked

- **WHEN** main thread 与 delegated child thread 分别 tracked 同一个 AGENTS 或 Skill source，随后文件变化
- **THEN** CSM 分别相对于各自 latest-visible-committed revision 做 reconciliation，并只向对应 thread append/seal；一个 thread rewind、untrack、dispatch failure 或 compaction 不得改变另一个 thread 的 source state 或 prefix

#### Scenario: child result 进入 main thread

- **WHEN** durable delegated child thread 完成并需要通知 main thread
- **THEN** main-thread owner 以带 source `session_id/thread_id/item/execution` provenance 的 ambient/runtime item 提交通知；不得共享 ContextStore、直接引用 child active view，或把 child 内容创建为真实 user Turn root

#### Scenario: thread locator 必须由 catalog 解析

- **WHEN** main thread 或非主 durable thread 执行 source reconciliation、assembly seal、rewind或dispatch
- **THEN** owner通过thread catalog/resolver取得受校验locator；main thread解析为`threads/{main_thread_id}`，其它thread直接解析为按不可变UTC创建日期分桶的`threads/YYYY/MM/DD/{thread_id}`且不增加hash shard，调用方不得自行拼接、扫盘或使用`checkpoint_ns`

#### Scenario: 未发布或不完整 thread 不能取得 Context owner

- **WHEN**Session+main或delegated child尚在creation staging，或缺少GraphBinding、capability、初始ContextStore、delegation seed/admission intent中的必需字段
- **THEN**CSM、history、before-model和侧边栏均不得解析或补写该thread；只有catalog单一事务发布的active thread可取得owner，重试按session/thread creation identity恢复同一main/child和初始execution

#### Scenario: 排队 execution 只在取得 active slot 后组装上下文

- **WHEN**同一child已有active execution，另一个真实用户Turn或内部wakeup已进入持久FIFO queue，排队期间tracked source或desired ToolSet发生变化
- **THEN**pending entry的queued root及entry-scoped seed/notice由旧execution的ExecutionContextFence按causal admission排除，不提前为该entry运行CSM或seal assembly；即使旧execution随后提交的tool/final item在物理sequence上晚于queued root，model/history仍按execution/Turn因果排序。与未来entry无关的ambient文件/Skill/team revision由ResourceRegistry异步发布，但默认`turn`边界下旧active Turn继续使用自己在active slot冻结的ResourceActivationSnapshot，只有显式`model_call`边界才在后续model-call preparation从Registry内存snapshot激活新revision；两者都不在请求路径读源。pending entry在前序终态、原子推进visibility并取得active slot后才冻结自己的execution snapshot并执行首次reconcile/rebase，sibling thread仍可并行

#### Scenario: 附件正文不成为 CSM source detail

- **WHEN**真实用户输入携带已持久化附件，或 Provider/tool需要读取附件正文
- **THEN** ContextStore只提交逻辑attachment/variant reference及thread/item provenance；正文由workspace attachment catalog在校验capability、owner/view membership、hash和length后读取，CSM、assembly detail和模型可见内容都不包含物理blob locator

#### Scenario: 附件发布与 Session 删除使用同一生命周期顺序

- **WHEN**已有`AttachmentOperationPin`的owner-reference提交与Session进入`deleting`竞争
- **THEN**二者由同一`SessionLifecycleGate`确定顺序：删除先行则拒绝未提交reference，发布先行则删除按pin阻断canonical使用、释放reference并收敛后再隔离Session；单次无锁active复核不得作为正确性依据

#### Scenario: CSM 与通信准入不能跨过 Session 删除栅栏

- **WHEN**owner rehydrate、resource activation/assembly seal、独立`skill_load`/ToolSet control、target inbox acceptance或其execution binding与同一Session删除并发
- **THEN**入口与删除竞争同一`SessionLifecycleGate`并持久化捕获generation的lease：入口先行则删除等待其terminal，删除先行则不得创建owner、source item、assembly、inbox、ambient item、wakeup或Job
- **AND**workspace catalog已deleting但部分local fence仍active的恢复窗口只允许workspace catalog metadata观察，新的thread history/detail和mutation返回`session_deletion_pending`，CSM不得把它解释成临时缺少owner后重建

#### Scenario: pending目录移动不重组上下文

- **WHEN** Web立即投影一系列Folder/Session移动，但Backend仅返回202或仍在队列中；随后同一main/child执行model call或从checkpoint恢复
- **THEN** lifecycle准入与CSM/ToolSet/GraphBinding只读取已提交SQLite Session locator及原SessionThread identity，sealed前缀byte不变，pending导航事件不成为source/item；后续导航命令committed也只更新breadcrumb而不触发context rebase

#### Scenario: 排队删除与已提交删除采用不同准入边界

- **WHEN** 递归删除已入队但尚未提交catalog deleting，另一业务入口请求target Session；随后删除worker提交整树deleting而部分local fence仍active
- **THEN** 前一时刻业务按原active catalog/fence正常准入并留下可恢复lease，后一时刻所有新业务按fresh catalog拒绝；Web是否已乐观隐藏子树或导航终态事件是否迟到都不能改变该顺序

### Requirement: resident runtime 卸载不得改变 durable context lifecycle

系统 SHALL 在 `add-itemized-rollout-context` 所定义的 Thread residency owner 获准卸载或重建 runtime generation 时，保持同一逻辑 ContextStore/CSM owner identity。该 owner 与 residency policy 唯一负责 idle 阈值、执行/lease/debug blocker、`ThreadResidencySnapshot`、cold read、GraphBinding 验证和 runtime generation；本 capability 不得重复定义这些行为。`LifetimeScope` 只释放可重建的进程内资源，不计算 idle、不停止进程级共享 watch 或跨 Turn 外部业务资源。canonical history、checkpoint/view、source registration/revision、latest-visible-committed 基准、tracking/untrack 状态、prefix epoch、ToolSet applied binding 和 sealed assembly 引用 MUST 在 unload/rebuild 前后逐字段保持不变。backend 重启时，context owner MUST 从该 Thread 的持久状态恢复；不得从当前源文件重写已提交上下文或让未核实的 owner 状态伪装成已恢复。

tracked source 变化 MUST 继续由 Resource Observation Platform 异步观察和 reconcile；runtime 只可在其配置的 resource activation boundary 消费 ResourceRegistry 内存 snapshot。unload/rehydrate 本身 MUST NOT 产生 source observation/item、主动读取源、重算 diff、推进 prefix epoch、改变 wire bytes，或使 snapshot/tracked/untracked 内容自动到期。

#### Scenario: runtime unload 保持已提交上下文字节

- **WHEN** Thread residency owner 按其唯一生命周期合同卸载某个 child 或 main thread 的 resident runtime
- **THEN** 最后已提交 assembly、stable-prefix hash/length、CSM revision、tracking state 和 ToolSet binding 逐字段不变，且不追加 runtime notice 或 source delta

#### Scenario: cold thread 在下一次 activation boundary 恢复 tracked source

- **WHEN**cold thread收到新消息并在execution admission重建runtime
- **THEN**系统在 itemized 定义的execution admission与generation建立后，按已提交registration、revision、active view和ToolSet binding恢复context owner；取得active slot后按配置从ResourceRegistry内存snapshot冻结Turn或model-call activation，并相对原latest-visible-committed基准决定是否追加delta，不主动检查tracked文件或从当前文件重写旧上下文

### Requirement: 生命周期场景必须进入统一 Web Integration 验收模块

本轮非协作的 main/child capability、CSM/Skill、resident runtime、fork／冻结来源／pinned retention 与附件释放重建 SHALL 由 itemized 原 test_conversation_lifecycle.py 的 Python owner 编排，复用真实 Gateway／Workbench 和既有隔离 fixture，不创建重复 E2E。测试 MUST 保留基础两轮 item／顺序／时长／刷新断言，并比较 cold/restart 的 GraphBinding、factory revision、sealed bytes 和 request_hash；DOM、API／history／SSE、trace 与持久事实须共同证明行为，不能只验证 helper。

本轮验收 SHALL 按已授权场景提供合取证据，使用原 model_stream 按 Thread／model-call identity 匹配和进程外超时／内存保护。外置 harness 只能控制时间或暂停，不写业务状态；工作区／BOXTEAM_HOME／端口隔离，teardown 仅关闭自身进程。已有准确原件 MAY 按源码依赖复用；包含延期 team／跨 Session 通信的完整 default MUST NOT 成为本轮前置，也不能将未跑场景算通过。

#### Scenario: 延期协作不阻塞非协作生命周期交付

- **WHEN** 非协作功能的实现与适用真实入口证据已逐项闭合，团队／跨 Session 通信仍等待新设计
- **THEN** 本轮可按明确范围同步与归档；延期设计和既有证据保留，不能把延期能力发布为已实现或为追求总门重跑无关场景

#### Scenario: 缺失真实入口证据仍不能完成

- **WHEN** 某本轮必做场景只有 helper、健康接口、文档校验或未经核对的历史收据
- **THEN** 该场景保持未完成，补与行为及风险对应的产品验证；不以延期拆分放宽身份、顺序、授权、事务或恢复断言
