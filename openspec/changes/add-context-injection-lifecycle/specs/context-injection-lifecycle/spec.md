## Purpose

为所有上下文 mutation 建立单一持久化/assembly owner，为动态 source 建立可审计且可重放的注入生命周期，并保证正常追加期间已提交 wire context 前缀字节级稳定，使 Skill 加载、文件变化、ToolSet hard rebase、rewind、compaction 和 Provider 投影不会重复、重排或伪造真实用户 Turn。

## ADDED Requirements

### Requirement: 已提交上下文前缀必须保持字节级稳定

系统 SHALL 把 Provider profile 下按 plan ordinal 排序并序列化的 wire context item frames 作为稳定前缀边界。在同一 prefix epoch 内，新 sealed assembly 必须完整保留父 assembly 的 context bytes，并且只能在其后追加新 item；不得修改旧 item 的 role、正文、content block、空白、编码、顺序或序列化。同一 epoch还必须绑定完全相同的 Provider projection profile、ToolSetRef和影响 root/tool visibility的 policy hash。只有首次组装、实际发生的上下文压缩重建、rewind重建和 ToolSet hard rebase可以建立不要求继承旧字节前缀的新 prefix epoch。

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

- **WHEN**SessionThread已有sealed assembly，用户或控制面把desired model/provider projection profile改为不同值，但没有实际compaction、rewind或有效ToolSet hard rebase
- **THEN**owner保持原applied profile和assembly，返回`provider-profile-change-requires-rebuild`且不dispatch；不得静默继续使用旧模型、创建第五种epoch reason或用no-op ToolSet变化伪造rebase

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

CSM/source producer MUST以 typed `SourceObservation`、`ApplySourceLifecycleDecision` 和 itemized `ContextContribution` 传递会影响追踪/激活/选择/替换的核心字段；`selection_role`、`replacement_policy`、`source_binding`、tracking state、desired/applied revision与 owner identity不能隐藏在自由 metadata/extension 中。只有 itemized owner-thread registry 可分配 `RegisteredContribution.source_ordinal`，CSM不得按观察事件顺序或内存计数补号。命名空间化、版本化的 `extensions` 可以透传或审计，但不参与 tracking、diff、selection、role、epoch、ToolSet或dispatch 决策；未知扩展不能因字符串 key 恰好等于旧控制 flag 而改变行为。任何新增生产代码需要影响核心行为时，必须先声明新的 typed capability/intent、owner、校验和测试。恢复时核心字段缺失或不一致必须显式失败，不从当前资源快照或扩展猜测；旧实验字段形态不提供 runtime alias/双读。

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

ToolSelectionStore/ToolService SHALL只拥有 workspace/agent级 desired selection及其控制面 revision；直接工具选择影响Provider ToolSet，扩展目标选择只影响ExtensionToolCatalog。每个 SessionThread ContextStore SHALL分别拥有最后观察到的desired及已applied ToolSetRef/revision、ExtensionCatalogBindingRef及其原始model-call binding。恢复或重放历史 assembly MUST使用两种已封存binding，不得用当前 desired selection反推历史工具集或扩展目标。

#### Scenario: model call 期间修改工具选择

- **WHEN** 一个绑定 ToolSet A的 sealed model call仍在 flight时控制面把 desired ToolSet改为 B
- **THEN** 当前请求继续只使用 A，系统把 B记录为 pending desired revision，并在下一个 model-call safe boundary之前不 seal使用 B的请求

#### Scenario: safe boundary 应用新 ToolSet

- **WHEN** 当前没有未收敛的 model/tool协议步骤且 desired revision不同于 applied revision
- **THEN** owner原子封存最终 desired ToolSet snapshot/ref、推进 applied revision、创建 `toolset_changed` prefix epoch，经 CSM reconciliation后重编译并 seal新的 root/messages/tools

#### Scenario: 切换时存在 outstanding tool call

- **WHEN** 旧 assembly已经产生尚未获得 terminal result的 tool call
- **THEN** 系统先按旧 ToolSet让已开始调用完成或返回真实失败；尚未执行且权限已撤销的调用返回绑定原 `tool_call_id`的真实 policy-denied result，全部配对收敛后才 hard rebase，且不得伪造执行成功或无配对取消

#### Scenario: safe boundary 前多次切换

- **WHEN** applied revision之后、下一次 safe boundary之前连续产生多个 desired ToolSet revision
- **THEN** 系统可以只为最终 desired revision创建下一 sealed snapshot，但必须保留控制面 revision因果审计，且不得覆盖任何已 applied/sealed ToolSet历史

#### Scenario: ToolSet 未发生有效变化

- **WHEN** desired ToolSet的规范化 snapshot和 policy hash等于当前 applied binding
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

### Requirement: 扩展目录、MCP指引和信封执行必须使用同一生效快照

系统 SHALL以固定的`invoke_extension_tool(tool_name, arguments)`作为全部非直接工具的唯一Provider信封，不保留`invoke_extension_tool`兼容别名。ExtensionToolCatalog SHALL把内置扩展、自定义及MCP target规范化为稳定identity、schema hash、目录revision和可审计权限策略；Provider ToolSetRef不得包含这些target的清单。McpCatalogOwner SHALL在启动、MCP `tools/list_changed`、重连和配置candidate切换时完整分页读取并验证`tools/list`，按语义变化发布不可变目录snapshot与独立channel通知；不具备变化通知的server只在显式刷新/重连或配置的有界轮询后承诺刷新，不得在model-call preparation执行网络请求。McpToolGuidanceProducer SHALL只从已验证目录派生有界、确定性的名称/描述/参数指引与删除tombstone，并明确注册为`root_placement=tail_only`的CSM source；原始MCP prompt/resource/instructions不得被动提升为指令。

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

每个source owner SHALL用typed `root_placement=root_eligible|tail_only`声明根指令资格，CSM不得从路径、文本或`extensions`推断。每个合法prefix epoch的最顶层root context SHALL至多投影为一个`wire_role=system` source item。首次assembly及实际compaction、rewind、Provider ToolSet hard rebase或fork目标首次assembly时，compiler MAY把仍在active view且被owner声明`root_eligible`的受信来源当前完整状态按确定顺序合并成该新root；`tail_only`来源，包括默认外部MCP工具指引，只能作为独立user-role数据。除此之外，第一条真实用户消息之后任何source完整内容、delta或恢复 SHALL作为独立`wire_role=user` item追加，不得改写、合并或前插旧前缀。新epoch物化必须记录base/delta lineage并避免重复选择，旧canonical item和sealed assembly保持字节不变。wire role不能改变canonical source identity、scope或Turn归属。

#### Scenario: 首次组装 root context

- **WHEN** Session/branch 在第一条真实用户输入前组装基础说明、初始 AGENTS 和 Skill metadata
- **THEN** 系统将这些初始贡献编译为一个最顶层 system wire item，并封存其 source provenance

#### Scenario: 用户消息后加载完整 Skill

- **WHEN** 模型在真实用户消息之后调用 `skill_load` 并读取完整 Skill activation
- **THEN** activation 作为新的 ambient source item追加到上下文尾部并投影为 user role，不前插或合并进 root system item

#### Scenario: rewind 或 compaction 完整恢复

- **WHEN** tracked source 在 rewind 后需要恢复当前完整 revision，或在 compaction 后物化为完整 revision
- **THEN** 只在实际新epoch重建时将`root_eligible`有效完整状态合并到新root；`tail_only`完整恢复仍是独立user-role item，旧base/delta只保留lineage且不重复投影；若无实际重建，全部恢复只能尾部追加user-role item

#### Scenario: Provider 原生工具 role

- **WHEN** Provider 协议要求 tool call 或 tool result 使用其原生 role/item type
- **THEN** 系统保留该协议 role；post-user user-role 规则仅约束 CSM source item

#### Scenario: ToolSet hard rebase 重编译条件化 root

- **WHEN** ToolSet变化使一个仅在特定工具 policy下启用的初始 instruction source改变 included状态
- **THEN** assembly compiler只在新的 `toolset_changed` epoch按owner的`root_placement`重编译最顶层root system item；`tail_only`和真实用户/工具协议事实仍保留其原有顺序与role，旧sealed assembly不变

### Requirement: 已知资源变化必须通过代码内装配的观察、快照和语义激活链路

系统 SHALL 对代码内明确装配的文件、Gateway受认证内部快照和权威内存来源提供统一的变化观察、稳定快照、解析与语义发布流程；typed适配合同可以供测试注入替身，但本change不得要求可安装资源插件、plugin manifest监视或动态执行任意provider/loader/reaction。已登记观察来源和已发布语义资源 MUST是不同身份与存储边界：`source_id`标识owner scope内的软件登记来源，`resource_id`标识可由一个或多个来源及其它语义资源派生的语义资源；路径、watch key、display URI均不得充当这两种身份。每个底层文件监视订阅 MUST 按monitor instance、规范化私有locator及recursive/filter/exclude/correlation/options等完整语义共享并引用计数，不能误合并不同观察合同，也不得为每个SessionThread建立独立watcher。资源观察、Job事件、配置生命周期和context source通知 MUST使用相互隔离的事件channel、queue、cursor、背压和故障域；资源事件不得进入job专属队列。

观察事件只能把已登记来源标记为dirty/change/gap/overflow，不能携带正文、credential或直接成为来源/语义revision。SourceReconciler MUST用已知来源适配的稳定一致性合同形成不可变`ObservedSourceRevision`：文件用允许根、稳定双读与完整原始byte hash，Gateway内部快照与权威内存状态用其可验证版本token，不能把后两者伪装成文件读取。ResourceDerivationGraph MUST从已接受的来源/语义依赖revision按无环拓扑、版本化loader、语义diff和CAS发布不可变`ResourceSnapshot`，并保留可验证source lineage；依赖环、缺失required依赖和跨generation不一致的多输入组合 MUST显式失败。同一来源可派生多个资源，多个来源可合成一个资源；原始来源变化但某facet语义payload不变时该facet不得推进revision或追加context item。重复、乱序、rename burst、丢失或overflow MUST通过有界reconcile收敛；失败时保留上一份valid snapshot并显式标记unavailable/diagnostic，required consumer不得使用旧valid替代当前失败。启动、Gateway内部快照重连和已知来源登记变更 MUST先完成已登记边界的initial reconcile并通过readiness gate；不得递归扫描未知目录、运行周期`rg`或根据物理目录猜测资源owner。

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

跨Turn terminal、browser、MCP、dev-server或Node调试进程的操作占用 SHALL由唯一持久operation lease账本记录typed资源身份、holder/operation身份与恢复状态；实际资源状态、停止、删除和恢复只由对应domain owner/provider核实并执行。Node调试必须用`(workspace_id, session_id, thread_id, debug process identity)`的typed`node_debug_process`资源身份，归属由debug owner校验而非模型参数决定。工具调用必须从typed tool/provider contract取得resource-use，不得在通用账本中按`terminal_id`、`pageId`等参数名推断资源或自动伪造登记。取消Turn、idle卸载和进程内scope close本身不得停止跨Turn业务资源；domain owner根据业务策略及所有有效lease决定停止还是只释放本次占用。旧内存stopper缺失、provider不可达、核实失败或仍有其它有效lease时不得把资源标记为`stopped`。`SessionResourceProviderRegistry`只路由列表/控制，不与业务owner或lease账本并行保存第二套资源状态。

Node调试的`debug process identity` MUST是每次启动唯一`process_instance_id`，而非PID、Inspector端口或单次tool/Web operation identity。debug owner须在spawn前durably登记绑定精确thread/lifecycle generation/launch preimage与一次性nonce的`launch_pending` claim，并在spawn后由nonce、OS进程起始身份和Inspector握手证明同一实例，才能登记PID/端口和运行态；该owner-held claim跨Turn持续，不因启动调用的短期`execution|debug_control` lease结束而消失。启动中崩溃只允许凭该claim/nonce核实原实例：证实不存在才结清，证实仍运行才接管或按domain策略停止，不可证明则保持`reconcile_required`及residency/delete blocker，不能认领/停止仅复用了PID或端口的其它进程。替换/重启必须先核实并结清旧实例，再建新实例claim；通用账本只保存身份、状态和证据引用，不负责搜索/杀进程或决定停止策略。

#### Scenario: 重启后缺少资源控制能力

- **WHEN**持久lease恢复后内存stopper不存在，且调用方要求停止外部资源
- **THEN**系统向实际owner查询并取得核实过的停止结果，或返回明确不可用/`reconcile_required`；不得只更新账本就向用户报告`stopped`

#### Scenario: 一个操作结束但其它lease仍有效

- **WHEN**同一外部资源被两个有效operation lease占用，其中一个Turn取消
- **THEN**只收敛该Turn的lease；除非资源owner在其业务协议下明确验证可停止，否则不能停止资源或自动释放另一holder的lease

#### Scenario: 活动Node调试进程的lease阻止闲置卸载

- **WHEN** debug owner有未结清的`launch_pending`进程claim、核实某thread进程处于`starting|running|paused|stopping`，或backend重启后无法确认旧进程是否已停止
- **THEN** 唯一外部资源lease账本保留该thread的占用/`reconcile_required`恢复事实，residency owner保持该thread的脱敏idle blocker而不宣称cold；只有debug owner核实`idle|exited|failed`并结清lease后才可重新起算30分钟，通用账本与`LifetimeScope`都不决定停止策略

#### Scenario: Node启动窗口崩溃与PID复用

- **WHEN** `launch_pending` claim提交后，backend在spawn前、spawn后登记进程属性前或停止确认前崩溃，重启时原PID/端口可能已复用
- **THEN** claim仍以原process_instance_id/nonce归属原thread，实际debug owner按OS起始身份和Inspector握手核实后才接管或停止；无法核实则保留`reconcile_required`与idle/delete blocker，不把PID/端口相同的其它进程标为原资源，也不由一次tool/Web操作lease终结推断资源已停止

### Requirement: Virtual Resource Namespace 必须隐藏locator并固定资源来源

系统 SHALL为可向模型或客户端展示的管理资源提供规范`boxteam://`虚拟资源URI。workspace AGENTS、workspace/Gateway/builtin Skill和memory资源 MUST分别使用可区分的逻辑scope/kind/name；URI中不得出现绝对路径、真实网络endpoint、credential、provider私有handle或正文。每个资源 MUST区分模型可见`display_uri`、内部稳定`source_id`、语义`resource_id`和provider私有`provider_locator`：URI只是locator/provenance，不是任一资源身份、授权凭据、dedupe key或业务幂等键，revision/hash/snapshot reference不得编码进URI。

URI resolver MUST在当前principal、workspace/gateway binding、resource activation snapshot和requested operation下返回typed resource handle，并重新校验resource capability；来源的`source_id`和provider私有locator只能从Registry-owned绑定解析，不能由URI、资源名或模型输入推导；不得把URI percent-decode后直接拼接为文件路径。parser MUST只接受已登记grammar，decode恰好一次，并拒绝userinfo、credential、未知query/fragment、控制字符、反斜杠、空segment、`.`/`..`、编码歧义和越界scope。旧context item、history或sealed assembly MUST使用当时封存的resource id/revision/hash/snapshot ref恢复，不能根据display URI重新读取当前资源。

`skill_load` MUST继续只接受`name + mode`。软件从当前冻结的SkillCatalogSnapshot解析exact resource handle，工具结果和context provenance可以返回安全`display_uri`，但该字段属于ResourceProvenance而不是Skill metadata；通用`read_file`不得通过虚拟URI或`.boxteam/.../SKILL.md`承担Skill activation/tracking。其它工具只有显式声明typed operation/capability时才可接受相应虚拟URI，不得提供绕过loader、Registry或CSM的万能资源读写入口。

#### Scenario: 模型理解Skill来源但看不到位置

- **WHEN** 模型按名称加载一个Gateway全局Skill
- **THEN** 结果和后续context provenance可以显示`boxteam://gateway/{gateway_id}/resources/skills/{skill_name}/SKILL.md`，但参数、结果、history和日志都不包含`${BOXTEAM_HOME}`物理路径、provider handle或credential

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

系统 SHALL只支持`turn`和`model_call`两种resource context activation boundary，默认值 MUST为`turn`，并允许按resource kind覆盖。queued Turn取得active execution slot时 MUST冻结一个`ResourceActivationPolicySnapshot`及有序`TurnResourceSnapshot`；policy snapshot保存revision/hash与每个resource kind的effective boundary，并在整个Turn内不可变，配置热更新只影响后续Turn。`TurnResourceSnapshot`固定全部turn-bound binding；若存在model-call-bound kind，每次tool protocol收敛后的安全model-call preparation MUST建立引用parent Turn snapshot的`ModelCallResourceSnapshot`，逐字节复用turn-bound binding并只重新冻结model-call-bound binding。已经sealed或in-flight的call始终不可变。

effective boundary MUST记录在每个resource binding中；系统不得以一个assembly级`boundary=turn|model_call`单值表示可能混合的资源。`skill_load` MUST使用产生该tool invocation的Turn/ModelCall snapshot解析Skill名称、entry与revision，不得在工具真正执行时改用更新的catalog snapshot。

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
- **THEN** 新ModelCallResourceSnapshot引用parent Turn snapshot，逐字节复用agent spec binding并只更新tracked Skill binding；每个binding分别标记effective boundary，assembly不得用单值boundary覆盖二者

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

系统 SHALL 从 bundled、`${BOXTEAM_HOME}/skills/` 和当前 workspace Skill 来源构造确定性的有效 catalog，并只向模型暴露唯一 Skill 名称及描述。workspace 同名项优先于 Gateway 全局项，Gateway 全局项优先于 bundled 项。模型不得看到或传入 Skill 的绝对路径、相对路径或 catalog locator。一个 `SKILL.md` 的 metadata 与 activation SHALL共享同一个已登记`source_id`，但拥有不同语义`resource_id`/facet identity并分别生效：安全frontmatter parser禁止custom tag/alias/object构造，name/description必须是唯一、有界scalar；其它字段不进入metadata/activation payload、facet revision或策略。activation只使用frontmatter结束offset之后的精确正文bytes。工具name只可作为已构造catalog map的受校验精确key，绝不得参与路径拼接。

SkillCatalog MUST由ResourceRegistry发布版本化immutable catalog snapshot/revision。bundled使用发布manifest；Gateway global和workspace file provider只监视并reconcile各自固定`skills/<entry>/SKILL.md`一层边界，拒绝越界symlink/未知递归且不运行`rg`。Gateway只返回带安全display URI、不含物理路径的descriptor，不写Session状态；资源变化异步更新catalog desired snapshot但不主动唤醒thread或广播context item，实际metadata delta只在该thread配置的activation boundary形成。

activation registration MUST 绑定`name + catalog_entry_identity + resolved_source_identity`。snapshot/tracked自动逻辑不得因同名优先级变化而改绑文件；原tracked source不可读返回`tracked-source-unavailable`并阻止未记录dispatch。显式再次`skill_load(name, tracked)`时，相同entry幂等复用，不同effective entry则在一个owner事务中冻结旧registration、追加当前完整activation并建立新tracking，返回安全`source_rebound=true`且不删除旧item。同source/revision已在active view可见的重复snapshot返回`already_active`且不追加；不同revision的显式snapshot可追加新immutable activation。tool invocation retry必须先按调用幂等identity恢复原结果。

由于`tracked-source-unavailable`会阻止模型dispatch，系统 MUST提供受信Session控制API，以精确`session_id + thread_id + normalized skill name + mode=untrack`调用和模型工具相同的CSM mutation。该API不得接受或返回路径、不得读取source、删除既有item、绕过权限/幂等/checkpoint版本化或维护第二套tracking状态；它不是新的Skill卸载工具。模型工具与控制API对`not_tracked`、`tracking-state-conflict`和成功untrack MUST返回同一安全状态合同，Web不得只改本地状态。

#### Scenario: Gateway 全局 Skill 可用于所有工作区

- **WHEN** `${BOXTEAM_HOME}/skills/<skill>/SKILL.md` 提供有效名称和描述且 workspace 没有同名覆盖
- **THEN** 该 Skill 出现在所有工作区的有效 catalog 中，但具体 Session activation 和 CSM 状态仍由工作区 Saver/ContextStore 持久化

#### Scenario: 同名 Skill 按固定优先级解析

- **WHEN** workspace、Gateway 全局或 bundled 层存在同名 Skill
- **THEN** 系统按 `workspace > gateway-global > bundled` 选择唯一 entry，并保持内部 origin provenance，不向模型泄露路径

#### Scenario: tracked 期间出现同名高优先级 Skill

- **WHEN**thread已tracked Gateway global Skill，随后workspace创建同名Skill并由ResourceRegistry发布新的effective catalog entry
- **THEN**metadata可追加catalog delta，但既有tracking仍绑定原source且不自动切换；只有模型显式再次调用`skill_load(name, tracked)`才原子冻结旧registration并加载/跟踪新entry，旧activation item保持不变

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

系统 SHALL 让 Skill metadata、Skill activation、AGENTS及其它文件 source由对应SnapshotProvider/SourceReconciler统一通过`StableSourceReader`读取，再由ResourceDerivationGraph产生语义facet。reader MUST 接受provider-owned `SourceReadHandle`与允许根；workspace source可由workspace provider读取，Gateway global Skill MUST由Gateway provider读取并通过仅内部可用、受认证且不含路径的`StableSourceSnapshot`向workspace发布entry/resource/source identity、来源revision hash、byte length和精确正文，随后按同一来源revision派生metadata/body语义resource。handle MUST 能在进程重启后由持久catalog entry identity重新解析，不得把临时进程token、物理路径或正文泄露给模型、普通工具结果或canonical history。CSM、middleware、`skill_load`和model-call preparation不得直接调用reader。

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

系统 SHALL 提供 `skill_load(name, mode)`，其中 mode 只能是 `snapshot`、`tracked` 或 `untrack`，缺省值为 `snapshot`。工具 SHALL 只接受模型可见名称；snapshot/tracked由软件通过当前ResourceActivationSnapshot中的SkillCatalogSnapshot解析内部resource/source identity与published snapshot，不能取得provider-owned handle/locator；untrack则按当前thread与受校验逻辑name定位唯一active tracked registration，不重新解析当前effective entry也不读取source。每个`(session_id, thread_id, normalized_skill_name)`至多一个active tracked registration；重复active状态返回`tracking-state-conflict`。工具结果只能返回安全display URI及revision/hash标识，不得暴露内部路径、provider locator或完整Skill正文。系统不得提供会立即删除既有Skill上下文的另一工具。

#### Scenario: 默认 snapshot 加载

- **WHEN** 模型调用 `skill_load(name)` 或显式使用 `mode="snapshot"`
- **THEN** 系统从当前activation snapshot取得已经稳定发布的完整Skill revision并追加一个不可变activation item，随后不注册tracking，也不在rewind移除后自动恢复；工具调用路径不重新读取文件

#### Scenario: 重复 snapshot 不产生同 revision 重影

- **WHEN**同一source revision的snapshot activation仍在active view，模型再次调用`skill_load(name, snapshot)`
- **THEN**返回`already_active`及原安全identity，不追加第二个item；若source revision已经变化，则该次显式调用可以追加新的immutable snapshot

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

系统 SHALL在rewind时从目标checkpoint恢复版本化tracking registration，按snapshot、tracked、untrack语义重建active view，并提交`PendingPrefixEpochTransition(reason=rewind)`；compaction提交summary/view时登记对应`reason=compaction` transition。pending transition没有wire bytes且不是applied epoch。下一次真正model-call preparation需要恢复/物化tracked source时 SHALL在同一owner事务中创建完整revision恢复事实、seal首个新prefix epoch assembly并消费transition；失败保留已提交view/transition但不追加半成品、不应用epoch或dispatch。同epoch或`tail_only`恢复/物化source必须是独立user-role item；只有真实新epoch中`root_eligible`受信source的完整有效状态可合并唯一system root。

任何会用于继续执行的rewind、compaction、replay或fork目标view SHALL在创建view或pending transition前验证tool protocol closure。若anchor位于assistant tool-call group与任一匹配terminal result之间，系统 SHALL返回`tool-protocol-boundary-conflict`、保持旧view且不创建目标或transition，并只返回无正文的最近安全anchor metadata；不得自动偏移、合成result、借source item闭合协议，或把只读partial history送入sealed assembly/Provider dispatch。

#### Scenario: rewind anchor 切入 tool 调用组

- **WHEN** rewind anchor保留assistant tool-call group却排除其中任一匹配terminal result
- **THEN** 系统返回`tool-protocol-boundary-conflict`，不改变active view、不创建pending prefix epoch transition，并向调用方返回可重选的安全anchor metadata

#### Scenario: snapshot 被 rewind 移除

- **WHEN** snapshot activation item 位于 rewind cutoff 之后
- **THEN** 该 item 从 active view 消失，系统不读取文件、不重新追加也不创建 tracking registration

#### Scenario: tracked revision 被 rewind 移除

- **WHEN** rewind 目标 checkpoint 仍包含 tracked registration，但其最新已注入 revision 不在重建后的 active view
- **THEN** 下一次真正model-call preparation从activation coordinator冻结的ResourceRegistry内存snapshot取得当前published revision，并将恢复事实、首个rewind epoch assembly与transition消费原子提交；`root_eligible`可进入新root，`tail_only`为独立user-role item；snapshot不可用时保留rewind view/pending transition但不追加item或dispatch，且不读取文件、网络或其它provider

#### Scenario: untrack 状态参与 rewind

- **WHEN** rewind 目标 checkpoint 位于一次 untrack 之前或之后
- **THEN** 系统分别恢复该 checkpoint 当时的 tracked 或 untracked 状态，不使用进程当前内存状态覆盖 checkpoint 因果顺序

#### Scenario: compaction 物化 tracked source

- **WHEN** active source 为 `A(base) + A→B(delta) + B→C(delta)` 且 compaction 实际重建上下文
- **THEN** compaction先提交view/pending transition，下一次真正model-call preparation把以C为完整内容的新revision、首个compaction epoch assembly与transition消费原子提交；`root_eligible`只在新root投影一次，`tail_only`仍为独立post-user user-role item，旧链保留审计引用

#### Scenario: compaction 处理 snapshot 或 untracked 内容

- **WHEN** compaction 遇到 active view 中的 snapshot 或 untracked Skill 内容
- **THEN** 系统只能使用已提交 item/detail决定是否携带，不重新读取 `SKILL.md`，CSM 不执行自动恢复

### Requirement: 生产上下文来源必须完整登记并由正确 owner 接管

系统 SHALL 维护可验证的生产上下文来源迁移闭包，并为每类来源明确唯一 domain owner以及进入统一 ContextStore mutation owner的 intent。Agent 基础说明、运行时身份/路径、条件化团队规则、Todo/Filesystem/Skill catalog/AGENTS/压缩工具说明以及显式启用的 memory 属于 instruction/file source；Goal、委派与分支结果、内部跨Session消息、团队动态事实、终端完成、模型重试控制和 checkpoint/runtime reminder属于 runtime event source。真实用户输入与附件、assistant/reasoning/tool协议事实通过 canonical append进入 owner；ToolSet通过 switch/hard-rebase进入 owner；compaction summary由 compaction owner产生。它们不得仅因参与同一模型请求而改造成 CSM source。生产请求不得包含来源闭包之外、没有 provenance的 system/user控制内容；模型工具不得把内部消息伪造为可信用户ingress。

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

#### Scenario: AGENTS 文件变化

- **WHEN** 共享file provider经SourceReconciler与ResourceDerivationGraph发布AGENTS语义revision B，而SessionThread已应用revision A
- **THEN** 系统在该resource配置的turn或model_call activation boundary登记A→B delta并以user role追加；原root/base不被原地替换，model request路径不读盘也不运行`rg`

#### Scenario: 通用 read 不得访问管理型 Skill 资源

- **WHEN** 模型通过通用文件读取能力提交Skill虚拟URI或旧`.boxteam/.../SKILL.md`路径
- **THEN** 系统明确拒绝该管理型资源读取；只有name-only `skill_load`能按冻结catalog snapshot注册activation，旧虚拟挂载和路径白名单不得保留

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
- **THEN** 同epoch或中途新增source统一输出独立user-role item；仅合法新epoch首个root可按owner的根指令资格重编译为system。Anthropic官方部分模型的中途system能力只保留未启用TODO

### Requirement: Context lifecycle owner 必须精确为 SessionThread

会话目录的pending客户端投影以及已durably accepted但尚未committed的`NavigationMutationRecord` MUST NOT成为SessionThread的context/source/ToolSet/GraphBinding事实，不得创建canonical item、触发CSM reconciliation、启动owner rehydrate或改变stable prefix。导航队列入队不等于Session lifecycle准入；真正应用目录命令时必须重新通过workspace catalog/topology gate，业务producer始终依据已提交catalog和Session fence。排队的递归删除在整树catalog deleting提交前不得阻止本来合法的业务；提交后即使部分local fence尚active或客户端仍显示旧树，所有新业务准入也必须拒绝。纯导航move/rename提交后只影响导航投影，不产生context epoch、source delta或模型请求。

系统 SHALL将ContextStore、CSM registration/revision/tracking state、active view、prefix epoch、ToolSet applied binding、sealed assembly与dispatch reference绑定为同一`(session_id, thread_id)` owner。Workspace `.boxteam/navigation/session-catalog.sqlite`是Session/Folder导航关系、`sessions/YYYY/MM/DD/{session_id}` locator与不可变main_thread_id的唯一权威；Session本地`session-control.sqlite`只拥有thread catalog（唯一main row须匹配workspace pointer）、collaboration ledger/fanout及publication journal，不得成为ContextStore/CSM或canonical writer。导航移动不搬Session node、不改fork/delegation lineage或已提交context，`session.json`不再保存可变父节点/当前显示名。

`session-control.sqlite` MUST保存唯一`SessionLifecycleFence(state=active|deleting|tombstoned, lifecycle_generation, deletion_record_id?)`。新Session持久副作用的准入先取workspace `NavigationTopologyGate` shared，再取至多一个目标`SessionLifecycleGate` exclusive，读取fresh SQLite catalog active/locator并确认local fence同generation，在一个Session control事务durably建立lease/等价record后释放；已有execution/runtime lease可覆盖其内部item/source/tool callback。最终可见性publication重取topology shared→原Session gate并fresh验证catalog active及token，catalog已deleting时新可见性发布必须取消、已准入lease只可收敛。导航parent/name调整仅由workspace SQLite/topology gate负责，不写thread context；所有lock顺序为topology→至多一个Session gate→至多一个SQLite写事务，不持锁跨模型/工具/网络或整个删除排空。

`SessionOperationLease` MUST至少持久化`lease_id`、`operation_kind=thread_creation|board_migration|collaboration_fanout|runtime_owner|execution|context_control|debug_control|communication_source|communication_target|federated_call|remote_observation|attachment|fork_retention|session_catalog_mutation`、稳定operation identity/preimage hash、captured lifecycle generation、holder generation/fencing token、`state=active|settling|completed|cancelled|failed`、revision和可选recovery ref；同generation/operation identity唯一且非终态可索引。专用record承担lease时 MUST以显式lease state或版本化全映射归一自己的preparing/routing/published/aborted等状态，不得按名称猜测。独立Web/API debug mutation须以`debug_control`准入，Agent工具可由其已有execution lease覆盖；该短期准入lease不替代跨Turn`node_debug_process`外部资源lease。lease不得墙钟自动到期。恢复owner验证旧holder generation失效并CAS新token后才可继续原operation或settle；fence deleting后不建新lease，旧lease只完成/取消冻结operation，不派生新root/wakeup/child。跨库主体先durable commit再terminal lease，中间崩溃按稳定identity/ref核对；删除请求settling后仍等待writer确认或幂等恢复核对，terminal后旧token callback必须失败。同一Session catalog目标条目的locator/lifecycle/归档mutation MUST以`session_catalog_mutation`竞争gate，旁路写入fail closed。

`SessionLifecycleGate` MUST按`(workspace_id, canonical_session_id)`使用workspace navigation根中的跨进程shared/exclusive OS锁；`NavigationTopologyGate`是该根中的独立跨进程短锁，所有Session准入先取shared topology。cold history/detail先在topology shared下获取fresh catalog locator和Session shared`SessionReadGuard`，释放topology后保持read guard到全部node/SQLite handle关闭并复核catalog/fence；catalog deleting前已开始reader可完成，新reader返回`session_deletion_pending`。删除owner逐Session关闭local fence前等待其read guard；锁文件不保存业务状态且不得unlink/recreate，不可验证时fail closed。

普通单child的`ThreadCreationRecord(state=preparing)` MUST在生命周期gate内、创建staging目录前提交到该数据库并承担operation lease，冻结最终/内部staging locator、artifact manifest/hash及Session lifecycle generation/catalog/collaboration precondition revision，最后重取topology shared/Session gate，并与catalog可见性在同一SQLite事务CAS验证workspace catalog active及fence仍为捕获的active generation且delegation、parent/member未漂移后推进为`published`；失败只定点清理并标记`aborted`，无预存record的目录不得被恢复路径吸收。board migration批量child MUST改由在gate内先行建立的`BoardMigrationRecord`内逐target的`MigrationChildCreationEntry`承担等价lease/generation/creation manifest，最终事务验证同一generation，且不得为同一target建立可由普通worker独立发布的`ThreadCreationRecord`。未指定thread的产品聊天/历史入口只能通过catalog明确解析main thread并返回实际ID。LangGraph `checkpoint_ns`只在已选thread内定位framework graph/subgraph checkpoint，不能取代product thread。

Session及其逻辑后代删除、`recursive=true` Folder删除统一按`add-itemized-rollout-context`的`NavigationSubtreeDeleteRecord`执行：在topology exclusive下从workspace SQLite递归冻结精确node/Session ID及每个日期locator，逐Session短时预检pinned retention，任一blocker即整批不标记删除；同一SQLite事务create-or-get幂等batch并一次CAS全树deleting。该catalog commit是所有新Session副作用准入与逻辑可见性的关闭点；旧独立`session-deletion-journal.sqlite`及先关local fence后更新JSON index的两阶段方案废弃。任一目标已漂移则整批冲突，不覆盖无关节点，不发布半棵树。

#### Scenario: pinned claim 与逻辑子树删除串行化

- **WHEN** pinned fork与包含source的Session/Folder递归删除同时尝试在各自准入边界提交
- **THEN** topology gate给出唯一顺序：claim先行则整树删除在catalog deleting前返回retention blocker、全部node仍active；删除先行则整树catalog deleting使claim零副作用失败，不能在local fence尚active时补pin

catalog整树deleting后，即使某Session local fence仍active，普通thread history/detail与新mutation也必须返回`session_deletion_pending`。删除owner按batch冻结清单逐Session取得一个Session gate、等待read guard、关闭local fence并收敛旧generation leases、child/board、copy attachment、communication与pin；旧writer只可按原lease和batch许可完成/取消冻结operation，不派生新root/wakeup/child。每个Session确认零非终态lease及引用释放durable后，把其日期目录定点隔离并记录进度，所有目标完成后workspace SQLite一次提交整树tombstone；崩溃保持全树deleting并只按batch manifest恢复，不能扫盘、局部开放、回退active或从已隔离目录重建CSM。

该owner identity MUST 先通过与item/storage change共享的完整canonical validator：`session_id`/`thread_id`分别具有36-byte ASCII `ses_[0-9a-f]{32}`/`thr_[0-9a-f]{32}`外形，且payload第13个hex为`4`、第17个hex属于`8|9|a|b`。CSM、ContextStore、Skill registration和assembly API不得接受非UUIDv4 bit profile、被清洗、截断、hash替代或通过旧ID path alias解析的identity；生产/测试IdentifierFactory必须复用同一validator，历史非规范ID只允许显式migration生成新的target identity和lineage。

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
- **THEN**pending entry的queued root及entry-scoped seed/notice由旧execution的ExecutionContextFence按causal admission排除，不提前为该entry运行CSM或seal assembly；即使旧execution随后提交的tool/final item在物理sequence上晚于queued root，model/history仍按execution/Turn因果排序。与未来entry无关的ambient文件/Skill/team revision由ResourceRegistry异步发布，但默认`turn`边界下旧active Turn继续使用自己在active slot冻结的ResourceActivationSnapshot，只有显式`model_call`边界才在后续model-call preparation从Registry内存snapshot激活新revision；两者都不在请求路径读源。pending entry在前序终态、原子推进visibility并取得active slot后才冻结自己的Turn snapshot并执行首次reconcile/rebase，sibling thread仍可并行

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

### Requirement: Session内部协作状态与跨Session协作事件必须使用不同生命周期

系统 SHALL 将Goal限定为Session main thread的能力；durable child thread MUST 不启用Goal，但仍拥有独立ContextStore、CSM、历史和真实用户Turn。team member、task、role和coordinator状态 MUST 只存于单个Session的协作状态中，并通过稳定member identity引用该Session的child thread；每个接收thread的CSM只能提交属于自己owner的相关状态revision/delta，不得共享另一个thread的source control state。

每次collaboration ledger mutation MUST先通过Session生命周期准入，并在同一`session-control.sqlite`事务中创建承担operation lease的`CollaborationFanoutRecord`，冻结fence generation、ledger revision、event hash、精确recipient thread集合和逐recipient delivery state；提交后不得按当前membership重算收件人。fanout worker按`(ledger_revision, recipient_thread_id)`幂等提交各owner的pending source observation，每次提交验证原lease/generation，不使用跨thread事务且不唤醒cold runtime。任一thread MUST在team_state配置的resource activation boundary冻结ledger revision为`source_reconciliation_snapshot`并从状态索引reconcile其中全部required revision；默认turn snapshot不得早于取得active slot时的ledger revision，不能使用queue acceptance时刻且同一Turn后续tool-loop不得漂移，只有显式`model_call`配置才可在后续call重新捕获。未seal连续变化可合并，已提交source item不可改写。崩溃、单recipient失败、cold或删除不得导致其它thread重复注入；fence关闭后未提交recipient写明确terminal delivery outcome且不得晚到append，snapshot内required revision未追平时禁止该thread dispatch。

跨Session协作 MUST 只通过面向目标main thread的显式send/read/wait操作。目标引用只接受裸`session_id`、`boxteam://session/{session_id}`、`boxteam://workspace/{workspace_id}/session/{session_id}`或`boxteam://gateway/{gateway_id}/workspace/{workspace_id}/session/{session_id}`；引用只是locator而非bearer capability。联邦部署 MUST支持一个中心Gateway hub通过SSH `-L`主动连接多个spoke，并在隧道内建立长期全双工WebSocket对等RPC channel；channel建立后hub与spoke均可主动发起request/response/event，不要求反向SSH隧道或spoke之间直连。跨spoke路径最多为`B → A → C`，只允许一个hub transit且不得继续多级转发。workspace-qualified link使用当前Gateway catalog identity，federated link使用稳定`gateway_id + workspace_id`；现有持久`connection_id`只作本地连接配置身份，瞬时channel instance/epoch/seq/ack和route locator均不得进入link、resolved target、GlobalThreadAddress、outbox/inbox preimage或业务幂等key。实际source Session/thread MUST贯穿中继，hub不得伪装成业务source。`send_message_to_session(target, content, kind="result", reply_to_communication_id?, delivery_policy="after_turn")`的`content`必须非空，`kind=question|reply|progress|result`，`delivery_policy=after_turn|after_tool_result|after_interrupt`；输入 MUST NOT暴露`communication_id`、`send_operation_id`或`simulate_user`，source outbox按软件持久化的稳定operation identity唯一分配communication，可信UI/API重试复用软件idempotency key。send MUST 由target workspace解析main thread并注册带真实source provenance的ambient/pending event和独立wakeup，不得创建目标真实user Turn。可信用户Turn只能由UI/API ingress owner创建。

send source outbox首次提交后 MUST 冻结稳定target GlobalThreadAddress；重试、source恢复、WebSocket/SSH重连或hub重启只可刷新指向该地址的临时route lease，不得重新用裸ID改投同名Session或因connection/channel/route revision变化改变communication preimage。稳定target不可达时保留原target并返回可重试路由错误。read/wait snapshot、selector和response envelope也只绑定稳定地址。hub丢失瞬时relay correlation后 MUST由source以原operation/communication重试并由target dedupe恢复，不得要求hub持久化消息正文或成为communication业务owner。

裸ID解析 MUST 使用有界exact-ID discovery：source查询自己的local workspace；spoke source通过唯一hub查询hub local workspace和其它active spoke，hub source直接查询自己的spoke。request携带完整`visited_gateway_ids`、`max_transit_gateways=1`、`max_gateway_hops=2`和总deadline；hub可fan-out一次，spoke只查本地且不得继续递归。lookup只读cold Session catalog且不加载runtime。只有在每个实际检查点按最新policy获准的候选参与解析；未授权存在与不存在统一为`target_not_resolvable`，多个已授权候选只返回不含locator的`target_ambiguous`/count并要求qualified URI。受认证、带catalog revision和短TTL的route hint不是业务事实或授权，目标workspace每次仍重新验证Session和main pointer。

远端裸ID discovery MUST 使用channel-bound origin envelope和独立的hub transit discovery grant。hub从source channel registration确认真实origin并拒绝冒充，再按最新transit policy向每个target spoke签发绑定issuer hub、origin gateway/thread、audience spoke、预期operation、canonical session ID、visited path、request/nonce/deadline的短期grant。target spoke验证受信hub、path和replay，并按自己的最新policy只在本地registered workspace cold catalog exact lookup；不得执行send/read/wait、读取history、解析child或继续递归。受认证response只能返回零、单个已授权qualified route hint或不含locator的ambiguity count；聚合唯一target后才进入完整operation授权。grant、response和hint都不得进入模型。

跨Gateway operation MUST 使用channel-bound origin envelope和内部、短期且受认证的hub transit grant。spoke B发起时hub A从channel binding确认`origin_gateway_id=B`，按最新transit policy检查后向C签发绑定issuer A、origin B、transit path `[B,A,C]`、audience C、不可逆principal ref、单一operation、规范target、可选source全局thread地址、稳定operation invocation、request/nonce/expiry的grant。C不需要与B直接配对，但 MUST验证已登记hub A、grant完整性/audience/path/期限/replay，并按自己的最新policy授权“来自B、经A”的principal。C的受认证target response由A验证后，A MUST以绑定origin request、target response hash和path的relay envelope返回B；业务source不得变成A。grant不得进入模型、link、canonical/source item、outbox/inbox正文或普通日志。send receipt额外绑定communication/payload/acceptance；每个网络attempt换request/nonce/grant但复用逻辑operation/communication，既有target dedupe为新grant认证原acceptance而不重复注入。

hub和target MUST 在任何lookup/forward前，分别把收到的origin/transit discovery/operation envelope以`(issuer, origin, audience, grant_kind, nonce)`原子写入Gateway control-plane持久first-use replay registry并绑定grant/request/path hash。同nonce不同preimage或重复first-use拒绝；网络结果未知时source使用新request/nonce/grant，send仍复用原communication。record保留到expiry、bounded clock skew与transport replay margin均越过，Gateway重启不得丢失有效窗口；registry、credential或key状态不可验证时fail closed且不产生workspace副作用。hub只能持久化peer/connection registry、policy/replay registry和脱敏audit，不得保存可替代两端outbox/inbox的业务状态。

Gateway federation权限的内置默认 MUST 对已认证、已登记在同一hub拓扑中的主体允许全部核心`discovery|send|read|wait|reply|transit`操作；限制规则为空且额外hardening默认关闭，用户无需先配置allowlist才能使用核心功能。有效权限候选 MUST校验、规范化为带revision/hash的不可变policy snapshot并原子热发布；无效候选明确失败且不得部分生效，已有channel不得仅因policy更新重启。policy不得进入模型上下文、ToolSet、canonical item或sealed assembly，工具保持可见并在实际调用被拒时返回明确authorization错误。

身份认证、channel identity binding、grant/response完整性、audience/path、防重放、target解析和业务幂等 MUST始终启用，不得被default allow或hardening开关关闭。每次discovery fan-out、hub transit、target operation admission、read分页、wait状态/terminal披露、send retry和hub relay response返回都 MUST读取对应Gateway的最新policy revision。远端wait准入时grant覆盖`effective_timeout + bounded_clock_skew`；cursor/selector不承载权限。运行中撤权 MUST阻止尚未durable acceptance的send并停止后续read/wait披露，返回`authorization_revoked`；已durable acceptance的send不回滚，后续read/wait重新授权。重新允许后下一次实际操作立即生效，不改写历史结果或上下文。

`read_context` MUST 只返回授权的有界history/summary projection、revision/cursor与source refs；当模型调用它时，返回值只能作为调用方thread的普通canonical `tool_result`，不得复制目标canonical item、Goal、CSM registration/control state或stable prefix。`wait_for_session` MUST替换`monitor_session_agent_end`并接受至多一个`communication_id|job_id|turn_id`selector、`until=terminal|state_change`（默认terminal）和`timeout_seconds`（默认60、范围1–300秒）。communication已经accepted但尚未绑定execution时，必须先等待binding再观察对应工作，不能返回虚假idle；无selector时冻结准入快照中已有的active/runnable/pending identity集合，不得订阅未来任意Job。结果状态闭集为`idle|pending|running|completed|failed|cancelled|timed_out`；timeout返回观察identity/current state与可复用selector，未知selector返回`selector_not_found`而非idle。read和wait MUST NOT进入目标CSM、创建目标context item、唤醒或materialize目标runtime。跨Session协议和通信账本不得保存、同步或推导team member、task、role、coordinator或Goal状态。

read首请求 MUST 分配稳定`observation_id/read_series_id`并签发版本化AEAD保护、opaque且可自验证的不可变`ReadContextSnapshot`/cursor envelope；密文内冻结该identity、resolved thread、active view revision、item/Turn上界、projection/visibility policy hash、总读取预算、offset和到期时间。模型或客户端不得读取/篡改内部字段，解密/认证失败统一返回不含内部细节的`read-snapshot-invalid`。token不写ContextStore/canonical history，target只可写独立受限的访问/operation control记录。后续页是新的source call/operation invocation，但通过cursor恢复同一observation/read series并继续该snapshot，且每页重新授权；目标变化不得混入，cursor不是capability。target/principal/policy/projection/limit冲突返回`read-snapshot-mismatch`，retention、认证key rotation或view/detail不可恢复返回`read-snapshot-expired`/明确loss，不静默切换当前view；旧cursor重放只能重读固定范围，不能绕过总预算。

read/wait每个source tool/API调用 MUST 使用其tool invocation或受信API idempotency key生成的稳定`operation_invocation_id`；它是本次`source_call_id`，不得兼作多页snapshot identity。source由现有execution lease覆盖或先在自己的Session gate建立`federated_call` lease，再在网络前create-or-get FederatedCallRecord并冻结operation、稳定target和参数hash；target先在自己的Session gate验证active fence并建立`remote_observation` lease，才可在ContextStore/canonical之外的Session-local有界operation store建立记录。read首调用以`(source_global_thread_address, first_source_call_id)`唯一create-or-get并分配`observation_id/read_series_id`，同call重试找回同一identity/snapshot；RemoteObservationRecord以observation identity保存冻结上界。后续页的新source call通过opaque cursor恢复该record，再以`(observation_id, page_ordinal)`唯一create-or-get绑定本次operation invocation与cursor hash的RemoteObservationPageRecord；一个source call只能映射一个page，同ordinal不同preimage必须冲突。wait observation以`(source_global_thread_address, source_call_id)`唯一建立并冻结selector集合、baseline、deadline和subscription identity。grant/response同时绑定source call及适用的observation/page identity，两端gate不得同时持有。目标删除先行时不创建snapshot/baseline/subscription；observation先行时删除把仍依赖node的调用收敛为明确target-deleted或原冻结结果并等待lease terminal。同identity不同preimage冲突，网络retry使用新grant但同一次source call复用该identity。source在target冻结后、保存response/tool result前退出时必须恢复同一page或wait terminal envelope，每个source call最多提交一个tool result；timeout后继续等待使用原selector、新operation identity和新observation。record过期返回`operation-retry-expired`而不静默重开。Session逻辑删除前 MUST terminalize全部非终态observation lease；catalog成为不可复用ID tombstone后，隔离节点及只读operation replay记录保留到全部未过期observation/communication恢复窗口结束，只允许匹配source address、source call、observation/page preimage且通过新授权的定点恢复路径访问，普通Session/history/runtime resolver不得打开。窗口结束后才可物理清理，之后原调用返回`operation-retry-expired`。该记录不得保存team/task/Goal、注入目标context或唤醒runtime。

#### Scenario: discovery grant 不能越权执行业务操作

- **WHEN** peer收到只绑定裸Session ID和预期operation的有效FederatedDiscoveryGrant，或同一grant在Gateway重启后被重放
- **THEN** 首次调用最多返回经本地policy过滤的受认证discovery结果，不能读取history或执行send/read/wait；重放由持久registry拒绝，且两种情况都不写workspace业务状态

#### Scenario: 默认配置直接允许核心跨 Gateway 功能

- **WHEN**B、C已通过中心Gateway A完成认证登记且用户没有配置任何federation限制规则或hardening
- **THEN**B可以经全双工channel和唯一hub transit对C执行discovery/send/read/wait/reply，系统不得因缺少allowlist或未显式开启安全选项而阻断；协议身份、完整性、路径、防重放和幂等校验仍然执行

#### Scenario: 运行中权限修改在实际使用点生效

- **WHEN**管理员在channel保持连接时原子发布新policy revision，依次撤销并重新允许B经A读取或等待C
- **THEN**已提交wire prefix、模型ToolSet和channel保持不变；撤销后的下一页、状态披露或新operation返回`authorization_revoked`且无新增目标副作用，重新允许后的下一次调用立即成功，已durable acceptance的send不被回滚

`WaitForSessionResult` MUST 返回resolved target、status、`observed[{selector_kind, selector_id, state, revision}]`、baseline revision和最新target revision。显式selector只包含该对象及communication到job/turn的binding；无selector空集合返回idle。非空集合 MUST 按`failed > cancelled > running > pending > completed`聚合且不依赖数组顺序；`until=terminal`等待全部冻结对象终态后返回failed/cancelled/completed，`until=state_change`在任一冻结对象revision变化后按相同优先级返回当时状态及完整observed。timeout只把顶层status设为timed_out，observed保留真实状态；调用方不得推导未返回对象。

wait MUST 由可注入单调`Clock/DeadlineTimer`和目标状态订阅驱动，生产不得轮询数据库。测试 MAY 推进虚拟时间验证默认60秒和最大300秒，但 MUST NOT 缩短产品合同或修改communication、Job、Turn、residency及canonical时间戳。deadline与terminal/state-change并发时，系统 MUST 读取target owner的已提交revision：条件已满足则返回对应状态，否则返回`timed_out`、该revision和可复用selector。

RemoteObservationRecord MUST 持久化版本化`DurableDeadline{timeout_seconds, admitted_at_utc, deadline_at_utc, monotonic_origin_id, monotonic_deadline}`。同一host boot/clock origin内的进程重启继续原monotonic预算；origin变化只能用可信UTC估算且不得超过原deadline/timeout。无法证明仍有正剩余、检测到回拨/超界或clock不可用时返回原baseline的`timed_out`/`deadline-clock-unavailable`，不得重新获得完整timeout。fake clock同时提供稳定origin和UTC映射，且与residency clock隔离。

跨进程/服务器send MUST 使用两个本地持久事实而非伪造分布式事务：source thread node拥有`CommunicationOutboxRecord`，target main-thread node拥有`CommunicationInboxRecord`；Gateway只能逐跳授权、路由和保存Gateway级访问审计，不得成为communication业务状态owner。两端记录都绑定source/target `GlobalThreadAddress=(gateway_id, workspace_id, session_id, thread_id)`、`communication_id`、immutable resolved target、payload hash、delivery policy和本端状态；outbox另绑定软件提供且模型不可见的`send_operation_id`并保存最新受认证target receipt，inbox保存target acceptance ref、ambient item/wakeup幂等键、`admission_id`、job/turn binding和terminal outcome。source outbox建立与target inbox acceptance MUST分别取得各自Session gate并在各自`session-control.sqlite`建立覆盖本端communication的轻量`SessionOperationLease`，两个gate不得同时持有；网络与双端提交只能组成可恢复saga。outbox只可`accepted → routing → target_accepted → execution_bound → terminal`，inbox只可`target_accepted → execution_bound → terminal`，任一侧可进入带原因的`failed|cancelled`；source只能以地址、communication、payload hash和acceptance ref匹配的受认证receipt推进远端状态。

每次逻辑send MUST 在route前取得稳定`send_operation_id`：模型工具调用绑定source execution/tool invocation，受信UI/API入口绑定软件生成并持久提交的idempotency key；模型不能传入该字段。source MUST先通过lifecycle gate确认active generation并建立communication lease，再以`(source_global_thread_address, send_operation_id)`在该lease覆盖的本地ContextStore事务中create-or-get outbox和唯一communication ID并冻结完整request preimage；同operation不同preimage冲突，lease/outbox中间崩溃按稳定identity定点继续或终结，outbox未提交不得route。source outbox和target inbox再分别以`(source_global_thread_address, communication_id)`dedupe；同key不同payload或target必须冲突。

target MUST先在自己的lifecycle gate内验证catalog/main pointer与active fence generation并建立target communication lease，再在该lease覆盖的同一ContextStore owner事务中提交inbox acceptance、唯一ambient item、wakeup幂等键和稳定`admission_id`；删除先关闭fence时不得建立lease/inbox，acceptance先行时删除必须发现并收敛该lease。JobService按`admission_id + preimage hash`create-or-get execution，并由仍有效的target communication lease覆盖或重新通过生命周期准入；Job创建后、binding提交前退出只能恢复原Job/Turn，不能在deleting generation晚到绑定。不得在event未提交时返回acceptance或在重启后创建重复Job。send只有收到target durable acceptance receipt后才返回成功；网络结果未知时要求用原ID或原send operation重试。target在acceptance与execution binding之间重启后必须从持久inbox/lease继续，source wait通过target查询或受认证receipt沿同一communication恢复。系统不得引入跨workspace共享SQLite、两阶段提交或Gateway业务账本。

communication不得用短TTL破坏dedupe。pending/running和reply/wait/audit需要的因果字段完整保留；terminal正文可按policy回收，但拥有者Session删除前保留包含地址、send operation/communication/acceptance/admission identity、payload/preimage hash、correlation、终态和receipt验证字段的最小tombstone。Session删除先将本地未终态记录收敛为带`source_deleted|target_deleted`原因的failed/cancelled并提交catalog deletion tombstone；Session ID不复用，迟到route不得改投其它Session。

target workspace MUST 使用单一`InboxAdmissionWorker`在acceptance提交事件及backend startup时，从持久状态索引恢复`target_accepted`且未`execution_bound`的inbox。worker按精确main-thread address、wakeup idempotency key和`admission_id`取得/rehydrate owner并幂等create-or-get admission，成功时提交原job/turn binding，永久失败时提交failed outcome和受认证receipt；不得扫目录、依赖内存future或创建第二ContextStore writer，并必须用claim/lease或等价约束避免并发重复admission。wait/read只能观察已提交状态，不得负责启动worker或唤醒目标。

#### Scenario: 未调用 wait 也会恢复 accepted communication

- **WHEN**target在acceptance后、execution binding前重启，且source尚未调用read或wait
- **THEN**InboxAdmissionWorker从持久索引恢复并按原wakeup key只admit一次；后续wait只观察同一binding/终态，不成为恢复触发器

消息`kind`闭集 MUST 为`question|reply|progress|result`。`kind=reply`必须提供`reply_to_communication_id`，且target inbox必须证明被回复communication的source/target与本次方向相反；其它kind携带该字段必须被拒绝。reply correlation不得成为跨Session共享task/team状态。

旧跨Sessionteam/member/task/coordinator数据 MUST 通过显式migration operation整块处理：要么从已终态或显式quiesce的legacy member main-thread checkpoint/view，调用migration-only `materialize_thread_copy`复用copy mapping/校验引擎，只在原coordinator Session的`BoardMigrationRecord`冻结staging中生成goal-disabled target-local child并保存legacy Session到child的lineage/mapping，要么freeze/detach旧membership并让原Session继续独立存在。该内部原语 MUST NOT创建新Session/main、写catalog/ledger、自行发布child或取得可执行owner；公开`full_rollout_copy`仍只创建独立target Session及main thread。source Session及历史保持只读不变；active execution、runtime、lease和未收敛mutation不得复制，无法quiesce时不得创建空child。

公开跨Session copy和`materialize_thread_copy` MUST在source shared `SessionReadGuard`下完成source catalog解析、node/SQLite读取并durably冻结绑定lifecycle generation、view/checkpoint上界、artifact/detail清单与hash的不可变snapshot manifest及staging bytes；释放guard后只能消费冻结副本，不得重新打开source。公开copy的target准入/发布不得与source guard重叠。board migration先短持coordinator gate建立`BoardMigrationRecord`/lease并释放，再按record顺序逐个source capture且一次最多持有一个guard，最后另行短持coordinator gate完成publication CAS；不得同时持有coordinator gate、source guard、另一Session gate或两个数据库写事务。删除先关闭source fence时copy/整批migration明确失败且零可见target；guard先行时删除只等待capture，释放后可继续且不等待target publication。

公开copy journal或每个`MigrationChildCreationEntry` MUST在capture前预登记唯一`source_snapshot_id`和正常resolver不可见的内部locator。capture MUST在source SQLite固定read snapshot内冻结同一revision的active view/checkpoint/control rows、`storage_commits`和各JSONL committed end offset，使用可验证SQLite snapshot/online backup复制数据库，并只复制offset以内JSONL以及manifest点名且hash/length/capability校验通过的thread-local immutable detail。完成时先在locator原子发布绑定operation/preimage、source lifecycle generation、database hash、逐文件offset/length/hash、view/checkpoint revision、detail manifest hash与attachment claim manifest hash的`SourceCopySnapshot(state=captured)`并完成durability barrier，释放guard后才CAS operation为`source_captured`。冻结后的source append/view变化不得混入；partial/prepared capture必须abort并定点清理，完整marker可以不回读source恢复。禁止扫盘、用当前source补齐旧snapshot或在同operation改用新revision。

workspace attachment正文 MUST NOT进入copy staging。source snapshot只冻结logical ref、digest/length/availability、source owner/item ref和attachment catalog revision，并以journal绑定的IdentifierFactory预分配target-local item/attachment identity与mapping。关闭source SQLite read transaction后且释放source guard前，copy在单个attachment catalog事务按`(copy_operation_id,source_attachment_ref,target_owner_ref)` create-or-get绑定snapshot/target/preimage的`ForkAttachmentClaim(preparing)`，原子验证source owner与blob、阻止GC；全部成功后才把有序claim ID清单/hash写入captured marker，中途崩溃按精确copy operation ID释放本operation claim。target/board发布前required claim必须`owner_reserved`并建立target owner ref，而resolver继续要求target Session/thread catalog active。有required claim的公开copy target MUST在staging control DB预置绑定copy/preimage、target lifecycle generation、publication preimage及claim ID/hash的`CopyAttachmentSettlementRecord(state=preparing)`，board由`BoardMigrationRecord`保存等价字段；publication把对应record推进为非终态`published_pending_attachment_commit`。无required claim的公开copy不创建settlement record，board publication直接进入终态`published`。finalizer随后单独取得target/coordinator gate、复核active generation/preimage，以不重叠的attachment catalog和session-control事务依次把claim committed，再把copy/board record分别推进成功终态`committed|published`。target删除先关闭fence时，排空流程必须按record精确释放owner_reserved claim/ref，或验证committed claim属于record冻结的target owner后按普通删除协议幂等、持久释放该owner ref，再把copy/board record分别推进终态`target_deleted|coordinator_deleted`，恢复不得重建或保留已删除target的owner ref或误报成功；finalizer先行时，删除在其释放gate后按普通owner ref合同处理。未发布失败仍按record claim ID释放并进入`aborted`。`CopyAttachmentSettlementRecord`状态闭集 MUST为非终态`preparing|published_pending_attachment_commit`和终态`committed|aborted|target_deleted`；`BoardMigrationRecord`状态闭集 MUST为非终态`preparing|published_pending_attachment_commit`和终态`published|aborted|coordinator_deleted`。非required且source已unavailable的历史ref可显式映射unavailable，required正文claim失败则copy失败。不得复制blob bytes、复用source identity、扫描digest补claim、留下claim到publication的GC窗口、使用无类型terminal标志猜测结果或在record非终态时隔离Session节点。

#### Scenario: copy attachment claim 跨 source 删除保持正文

- **WHEN**source guard内已建立attachment claim，随后source删除释放原owner，而target/child仍未发布
- **THEN**claim继续保护workspace唯一blob；只有target/child catalog发布后reserved owner ref才可被resolver使用，恢复按claim ID提交或释放且不产生悬空ref/重复blob

#### Scenario: target 删除不与 copy attachment finalization 交错

- **WHEN**target/board已经发布但claim仍为owner_reserved，target/coordinator Session删除与finalizer竞争同一gate
- **THEN**finalizer先行时完成全部claim commit并把copy/board record分别推进成功终态`committed|published`后才释放gate；删除先行时排空流程释放reserved claim/ref，或验证committed claim归属后持久释放其target/child owner ref，再把copy/board record分别推进终态`target_deleted|coordinator_deleted`，后续copy recovery不得重建或保留owner ref、恢复active或把已删除target报告为可用

`pinned` fork MUST在capture前建立source-local durable retention claim。target creation/fork journal先预分配operation/preimage，随后在不持target gate/事务时先取得workspace topology shared gate、再取得source exclusive gate，于source `session-control.sqlite` create-or-get绑定fork、两侧GlobalThreadAddress、target operation/preimage、source generation与view/detail范围的`ForkRetentionClaim(state=preparing)`和retention占位。source删除先提交catalog deleting则claim不得建立；claim先提交则删除 MUST在整树catalog deleting前返回`source_retention_operation_pending|source_retained_by_fork`。target提交后另取source gate只激活同一claim，不能首次补写。abort/target删除先持久化target release intent并释放target gate，再单独取得source gate释放claim，最后确认target terminal；preparing claim无墙钟过期，record缺失/冲突fail closed。copy/fork仅限同一workspace，federated grant不得执行远端fork、打开远端业务库或修改retention。

`ForkRetentionClaim`自身 MUST按`operation_kind=fork_retention`承担完整Session operation lease并与retention占位原子提交，不得另建平行lease。

#### Scenario: pinned claim 先于 source capture

- **WHEN**pinned fork即将读取source，或source删除与其并发
- **THEN**只有已提交且preimage一致的preparing claim才能开始capture；删除先提交catalog deleting时fork零可见副作用失败，claim先提交时删除不提交catalog deleting并返回明确retention blocker

#### Scenario: target 删除通过跨库 saga 释放 pin

- **WHEN**pinned target删除或fork abort
- **THEN**target先durably记录release intent并释放本地gate，再单独取得source gate释放精确claim，之后确认target terminal；任一崩溃点幂等恢复且不同时持两端gate/事务

迁移 MUST使用不可见staging和coordinator Session的单一可见性提交点，而不是跨thread/workspace分布式事务。在创建任何child staging目录前，系统 MUST先于`session-control.sqlite` create-or-get不改变thread catalog/collaboration ledger的`BoardMigrationRecord(state=preparing)`，冻结operation/preimage、coordinator lifecycle、旧board/catalog revision，并为每个target内嵌唯一`MigrationChildCreationEntry`，包含child ID、最终/内部staging locator、GraphBinding/capability、source checkpoint/view、lineage/mapping及预期artifact manifest/hash；同operation不同preimage MUST冲突。该entry MUST是batch child唯一creation journal；普通ThreadCreationWorker MUST NOT枚举、发布或启动它，也不得为同一target建立独立`ThreadCreationRecord`。随后才验证并durably flush记录内的全部child artifact、mapping、权限和hash，原子rename到记录冻结且尚未被catalog引用的最终locator；全部rename成功后，同一个SQLite事务CAS验证coordinator仍active、旧board/catalog revision及member/task preimage未漂移，再同时更新thread catalog、collaboration ledger全部locator/board/member/task mapping；无required attachment claim时record直接进入终态`published`，否则进入非终态`published_pending_attachment_commit`并在claim结算后转为`published`。该事务是唯一可见性提交点，后续结算不得改变board成员集合。CAS失败 MUST不覆盖并发变更、不重基或部分发布，只定点清理后标记`aborted`。任一rename失败同样不得发布；rename后、发布前崩溃的orphan因preparing record已先存在而可按其有限target定点校验、继续或清理，禁止扫盘或吸收无record目录；发布后、terminal response前恢复同一结果。发布前正常reader只见旧board，发布后只见完整新board。不得形成部分board、同时指向外部Session和本地child的混合member，也不得由正常runtime解释staging或未迁移的旧状态。

#### Scenario: board migration child 不能被普通 worker 单独发布

- **WHEN**一个`BoardMigrationRecord`包含多个`MigrationChildCreationEntry`且普通ThreadCreationWorker同时运行
- **THEN**普通worker无法认领、发布或启动任一migration child；只有board最终事务能同时发布全部child及member/task mapping

#### Scenario: legacy board 迁移保留旧 Session 上下文

- **WHEN**迁移选择把一个已quiesce的legacy member Session物化为coordinator Session的child
- **THEN**`materialize_thread_copy`在record冻结的不可见staging中给child生成target-local history与lineage，Goal保持禁用；它不创建新Session/main或自行发布，原Session、用户历史和导航不变，不能只创建无历史空child或复制active runtime

#### Scenario: migration source capture 与删除双顺序

- **WHEN**migration先取得source Session的shared `SessionReadGuard`
- **THEN**删除等待source snapshot capture完成；guard释放后migration只用冻结manifest/bytes而删除可继续，不要求source存活到board发布
- **WHEN**删除先关闭source fence
- **THEN**capture返回`source_session_deletion_pending|source_session_deleted`，整个`BoardMigrationRecord`不发布并定点abort/清理

#### Scenario: copy 锁阶段不得嵌套

- **WHEN**公开`full_rollout_copy`创建target，或board migration依次读取多个source并发布coordinator
- **THEN**source guard、target/coordinator gate和数据库写事务按阶段串行取得，任意时刻最多一个Session gate/guard与一个数据库事务；违反顺序时fail closed

#### Scenario: copy source snapshot 使用同一 committed revision

- **WHEN**source在capture固定SQLite read snapshot和JSONL committed offsets后继续提交item或切换view
- **THEN**目标只消费冻结revision的SQLite、offset内JSONL、checkpoint与detail manifest，不能形成跨revision混合副本

#### Scenario: copy capture 崩溃恢复不扫盘

- **WHEN**capture在partial staging或captured marker到operation CAS之间崩溃
- **THEN**恢复仅定点检查预登记`source_snapshot_id`/locator，partial必须abort/清理，完整且hash一致的snapshot继续；不得扫描目录、回读当前source补齐或替换revision

#### Scenario: legacy board staging 崩溃不暴露部分状态

- **WHEN**migration在child staging完成后、coordinator发布事务前崩溃，或在发布提交后、staging清理前崩溃
- **THEN**恢复分别只暴露完整旧board或完整新board，并按journal继续/清理；正常API与CSM永远看不到staging child、混合member或跨库半提交状态

#### Scenario: legacy board 并发变化使迁移失效

- **WHEN**preparing record提交后、发布事务前，coordinator lifecycle、旧board/catalog revision或member/task preimage发生变化
- **THEN**CAS拒绝发布且并发变化保持不变；恢复只枚举record冻结的target完成清理并标记`aborted`，CSM不得把staging source或child注册为active owner

#### Scenario: team fanout 中途崩溃后逐 recipient 恢复

- **WHEN**ledger revision及冻结recipient集合已经提交，但进程只向部分thread提交pending observation后退出
- **THEN**重启worker只补齐未完成recipient；已完成thread不重复追加，cold thread不因fanout加载runtime。recipient只在自己的下一次resource activation boundary消费已发布snapshot：默认`turn`在取得active slot时追平，显式`model_call`在对应call preparation追平

#### Scenario: team 变化遵守配置的 activation boundary

- **WHEN**child排队期间发生ledger revision R2，取得active slot后首次model call前发生R3，第一次tool call后第二次model call前又发生R4
- **THEN**queue acceptance不冻结过早上界；默认`turn`在取得active slot时冻结当时最新可用revision且整个Turn不漂移，R4由下一个Turn消费。仅当`team_state=model_call`时，第二次model-call preparation才从Registry内存snapshot激活R4。每个revision按facet lineage至多提交一次，任何旧sealed request都不改变，两个分支均不读取ledger或源文件

#### Scenario: child thread 不接受 Goal 状态

- **WHEN** Goal producer或API试图为durable child thread创建、恢复或更新Goal source
- **THEN** 系统返回明确的capability/target错误，不向child追加Goal item，也不得把操作静默改投main thread

#### Scenario: Session内部团队任务更新

- **WHEN** 当前Session的team ledger把任务分配给一个child-thread member或更新其状态
- **THEN** 系统持久化Session内因果revision，并只向需要感知该变化的精确thread提交各自ambient source；不得创建跨Sessionmember、普通user Turn或共享CSM registration

#### Scenario: 跨 Session 消息进入目标 main thread

- **WHEN**来源thread通过Gateway向另一个Session发送协作消息
- **THEN**目标workspace解析该Session的main thread，并由main-thread owner提交带真实source thread与communication provenance的pending event；两个Session不产生共享team/task/Goal状态

#### Scenario: Agent 不能伪造真实用户入口

- **WHEN**模型检查或调用`send_message_to_session`
- **THEN**工具schema中不存在`simulate_user`，消息只能成为目标main thread的内部ambient/pending event；只有受信UI/API ingress可创建目标真实user Turn

#### Scenario: 跨 Session read 和 wait 不注入上下文

- **WHEN**调用方读取目标Session main-thread历史，或通过明确selector等待目标工作
- **THEN**read只返回有界projection及source refs并可成为调用方tool result；wait只返回所观察identity的idle、terminal、timeout或cancel状态；目标CSM、canonical history、prefix epoch和resident runtime均不因read/wait改变

#### Scenario: read 分页区分 source call 与 observation identity

- **WHEN**调用方用首个read tool invocation取得opaque cursor，再以第二个tool invocation读取下一页
- **THEN**两个调用具有不同`operation_invocation_id/source_call_id`和各自至多一个tool result，但cursor必须恢复同一`observation_id/read_series_id`、snapshot revision与上界；不得以第二次tool invocation创建新snapshot

#### Scenario: send 后立即 wait 不误报 idle

- **WHEN**send已返回`communication_id`，但目标消息仍处于accepted/queued且尚未绑定Job或Turn，调用方立即以该communication等待
- **THEN**`wait_for_session`先跟踪同一communication到execution binding，再观察该工作至有界终态；不得因查询瞬间没有active Job而返回idle完成，也不得订阅无关的未来Job

#### Scenario: wait 超时返回可恢复 selector

- **WHEN**wait在默认60秒或显式1–300秒预算内未达到terminal或state-change条件
- **THEN**返回`timed_out`、resolved target、观察identity/current state和原selector；调用方可复用它继续等待，不创建新communication或订阅无关Job

#### Scenario: fake deadline timer 保持产品等待合同

- **WHEN**验收推进注入的单调fake timer到默认60秒或最大300秒，且目标可能在deadline边界提交状态
- **THEN**系统不真实sleep、不缩短配置、不轮询数据库，并按target已提交revision裁决终态/状态变化或`timed_out`；虚拟时间不得改变其它业务时钟或residency

#### Scenario: target acceptance 后重启恢复同一通信

- **WHEN**测试harness在target进程外持有仅fixture可注入的`CommunicationAdmissionBarrier`，target经内部test port在durable接受communication后、execution admission前暂停；重启后的target重连同一barrier，harness释放后先在source未调用read/wait时观察只读communication/trace证据，再以同一communication ID重试并继续wait
- **THEN**InboxAdmissionWorker主动恢复原acceptance ref并只注入一次ambient event、补齐同一job/turn binding；wait只观察既有恢复结果，同ID不同payload或target明确冲突

#### Scenario: source 在 acceptance 后丢失 receipt

- **WHEN**target已提交acceptance而source在持久化receipt前退出，随后恢复同一tool/API invocation
- **THEN**source按`send_operation_id`找回原outbox/communication，以新grant取得原acceptance的认证receipt；target不重复创建inbox、ambient item、Job或Turn

#### Scenario: admission 与 binding 之间退出

- **WHEN**JobService已create-or-get execution但inbox尚未提交job/turn binding时target退出
- **THEN**worker按原`admission_id`恢复同一Job/Turn并补写binding，不创建第二execution

#### Scenario: terminal正文回收后仍可判定重试

- **WHEN**terminal communication正文已回收但拥有者Session仍存在，随后发生相同ID重试或同ID不同preimage冲突
- **THEN**最小tombstone返回原终态或明确冲突，不重新注入；删除的canonical Session ID不得被复用

#### Scenario: target 删除后仍可重放已接受 observation 的终态

- **WHEN**target已接受read/wait observation并生成终态，但source在保存response前退出，随后target完成逻辑删除且source以同一source call和新授权重试
- **THEN**普通resolver仍只看到不可复用ID tombstone；定点恢复路径在retention窗口内从隔离节点返回相同terminal envelope且不重开history/runtime，窗口结束后返回`operation-retry-expired`并允许物理清理

#### Scenario: reply correlation 不接受伪造方向

- **WHEN**`kind=reply`缺少`reply_to_communication_id`、引用不存在/未授权的communication，或被引用communication的source/target方向不与本次相反
- **THEN**系统在创建outbox/inbox和ambient item前拒绝；非reply kind携带该字段也必须拒绝，不按文本或裸Session ID猜测关联

#### Scenario: 跨工作区 Session link 每跳重新授权

- **WHEN**spoke B使用Gateway-local或带`gateway_id`的federated Session URI，经中心Gateway A路由到spoke C上的workspace执行send/read/wait
- **THEN**B的origin channel、A的唯一transit和C的target admission分别读取最新policy并返回resolved gateway/workspace/session/main-thread identity；链接不携带凭据或connection/channel ID，C看到的真实source仍为B，歧义、未知gateway、显式deny或不可达明确失败

#### Scenario: 联邦授权错误不产生目标副作用

- **WHEN**即使权限默认允许，远程请求仍携带错误origin/audience/transit path、过期或重放grant、失效channel identity/peer credential，或命中运行时显式deny rule
- **THEN**hub或target Gateway在workspace转发前拒绝并只记录脱敏decision audit；目标inbox、context、runtime与source远端状态均不推进，模型和普通日志无法看到grant或credential

#### Scenario: 旧跨 Session team 状态不会形成混合成员

- **WHEN**migration读取一个以delegated Session表示成员的旧team board
- **THEN**整个board选择由`materialize_thread_copy`从终态/quiesce source main写入不可见target child staging，或freeze/detach旧membership；只有全部验证后才由coordinator catalog/board本地事务一次发布，公开copy语义不变，且不得出现空child、部分task/member、跨库半提交或外部Session与child混合身份

### Requirement: resident runtime 卸载不得改变 durable context lifecycle

系统 SHALL 允许durable SessionThread的resident runtime被卸载和延迟重建，同时保持同一逻辑ContextStore/CSM owner identity。当前child thread默认在无active/runnable/pending execution、未收敛model/tool/mutation和runtime lease、debug owner已核实无`launch_pending`进程claim、`starting|running|paused|stopping`进程及`reconcile_required`阻断且连续30分钟无活动后进入cold状态。调试进程活动期间不累计idle时长；终态核实和lease结清后重新起算完整30分钟。residency manager决定关闭目标runtime generation的`LifetimeScope`，但不能自行推断debug状态；scope只释放可重建的进程内资源，不计算idle、不停止进程级共享watch或跨Turn外部业务资源；canonical history、checkpoint/view、source registration/revision、latest-visible-committed基准、tracking/untrack状态、prefix epoch、ToolSet applied binding和sealed assembly引用必须保持不变。backend重启若调试占用仍在恢复/状态不明，应先恢复/核实owner并维持loading或带阻断的resident状态，不得直接宣称cold。

系统 SHALL 提供只读`ThreadResidencySnapshot`，至少包含`session_id`、`thread_id`、`residency=cold|loading|resident|unloading`、execution状态、`last_activity_at`、`idle_deadline_at`和脱敏`blocking_reasons[]`。该snapshot只描述runtime residency，不得成为canonical item、CSM source、team state或模型上下文。residency manager MUST 通过可注入单调`Clock`计算deadline：生产使用真实时钟，测试使用fake clock验证真实30分钟阈值，不得实际等待30分钟或降低产品阈值。

list/history/detail读取 MUST 走cold path且不得创建可写runtime。下一次execution admission MUST 根据持久ThreadRuntimeBinding和GraphBinding获得唯一新runtime generation，逐字段验证原`graph_id、graph_revision、graph_schema_hash、capability_profile_hash`后再恢复ContextStore/CSM；进程缓存丢失或factory registry已有更新时也不得改用latest graph。tracked source变化始终由Resource Observation Platform异步监视和reconcile；runtime只在随后配置的resource activation boundary消费Registry内存snapshot。unload/rehydrate本身不得产生source observation/item、主动读源、重算diff、推进prefix epoch、改变wire bytes或让snapshot/tracked/untracked内容自动到期。

#### Scenario: idle unload 保持上下文字节稳定

- **WHEN**符合条件的child thread在30分钟idle threshold后卸载resident runtime
- **THEN**最后已提交assembly、stable-prefix hash/length、CSM revision、tracking state和ToolSet binding保持逐字段不变，且不追加runtime notice或source delta

#### Scenario: fake clock 精确验证 idle 临界点

- **WHEN**测试在无阻断lease的child thread上将可注入Clock推进到最后活动后的29分59秒，再推进到30分钟整
- **THEN**`ThreadResidencySnapshot`先保持resident并给出deadline，随后转为cold；若存在active/pending execution、未收敛mutation或lease，则30分钟整仍不得卸载并在`blocking_reasons`中给出脱敏原因

#### Scenario: 调试进程跨越30分钟不进入cold

- **WHEN** child在无execution时保有`launch_pending`进程claim或保持`starting|running|paused|stopping`Node调试进程，fake clock跨过30分钟，随后debug owner核实终态并结清lease
- **THEN** 活动期间residency保持resident且有脱敏debug blocker；终态后从零重新起算30分钟，停止失败或重启状态未知时保持`reconcile_required`阻断，不因旧scope消失误报cold

#### Scenario: cold history 读取不创建 owner 实例

- **WHEN**用户只查看cold child thread的历史或来源详情
- **THEN**系统从持久投影返回结果，不创建model/tool client、compiled graph、writable ContextStore/CSM runtime、execution或assembly

#### Scenario: cold thread 在下一次 activation boundary 恢复 tracked source

- **WHEN**cold thread收到新消息并在execution admission重建runtime
- **THEN**系统先建立新runtime generation，逐字段恢复并验证原GraphBinding以及已提交registration、revision、active view和ToolSet binding；取得active slot后按配置从ResourceRegistry内存snapshot冻结Turn或model-call activation，并相对原latest-visible-committed基准决定是否追加delta，不主动检查tracked文件、不改用latest graph或从当前文件重写旧上下文

#### Scenario: 迟到 callback 不写入旧 runtime

- **WHEN**thread卸载后收到持久后台资源或终端的完成callback
- **THEN**callback使用保存的精确`session_id + thread_id`取得当前runtime generation或触发精确wakeup，并经唯一owner提交；旧实例和Session main-thread fallback都不得接收该mutation

### Requirement: 生命周期场景必须进入统一 Web E2E 验收模块

本change的main/child capability、CSM/Skill lifecycle、resident-runtime、跨Session send/read/wait、本地copy/source snapshot、pinned retention/delete、copy attachment claim/GC saga及stable-prefix断言 MUST 写入由`add-itemized-rollout-context`统一拥有的`tests/e2e/clients/web/test_basic_chat_tool_loop.py`，不得创建语义重复的第二套Web E2E。cold/restart场景 MUST比较持久GraphBinding四元组及实际factory revision，证明新runtime generation仍解析原binding而非latest graph。共享Python/Node helper MAY 被调用，但pytest collection、场景编排和PASS/FAIL gate必须位于该Python模块。pytest MUST 拥有外置`E2ETestControlHarness`，分别提供residency/wait单调时间域，以及按operation/phase寻址的`CommunicationAdmissionBarrier`、`SessionLifecyclePhaseBarrier`和`CopyCaptureBarrier`；后两者覆盖deletion journal/fence/final rename、source revision freeze、attachment claim/captured marker/owner_reserved、target/board publication及attachment finalization。测试backend只通过fixture composition的内部port连接，生产composition只绑定真实时钟和立即返回的no-op observer/barrier，不得注册test client、HTTP/model入口或热开关。barrier只暂停并上报phase，不改业务状态/identity/time/result；pytest收到ack后从进程外终止/重启，backend不得由test hook自杀。harness不得写业务库/checkpoint/canonical时间。由此保持正式30分钟及60/300秒产品合同而不真实等待。A/B/C Gateway必须作为三个独立进程，分别使用测试输出目录内隔离`BOXTEAM_HOME`、测试peer credential与policy snapshot并组成真实loopback `B ⇄ A ⇄ C`全双工channel；必须覆盖默认允许、连接中撤权、恢复权限和跨spoke response原路返回，不得用反向SSH、B/C直连、resolver stub或remote-backend直连冒充。模型回放必须按SessionThread/model-call identity匹配，禁止并发thread共享顺序游标或访问真实Provider。验收联合验证DOM、API/通信账本、thread history/SSE和runtime/trace证据，不得接触用户正常全局目录；单一DOM文本或单一后端状态不得独立判PASS。

测试composition MUST 通过pytest fixture注入确定性但仍满足UUIDv4 bit/profile的`IdentifierFactory`，生产composition只绑定随机UUIDv4 factory；Session/child仍经正常Gateway/Web API创建，不得预写业务库。ModelStream manifest必须精确匹配这些动态identity和model-call identity。该模块 MUST 属于独占serial E2E组，并通过输出目录内带PID/start-time验证的`E2EProcessLease`租用避开8010–8016的整套loopback端口与owner manifest；不得杀死未知监听者、抢占有效lease或共享开发Gateway home。stale lease仅在验证owner不存在后恢复，case使用独立fixture子目录/seed，teardown只关闭本次owner并断言端口释放。

#### Scenario: lifecycle 验收复用 itemized 的单一场景所有者

- **WHEN**实现本change的CSM、child runtime和跨Session工具迁移
- **THEN**开发者扩展同一个`test_basic_chat_tool_loop.py`中的共享fixture和分阶段pytest用例，保留基础两轮tool-loop断言，并加入本change的生命周期证据；未被该Python模块收集的辅助脚本不能作为唯一验收入口
