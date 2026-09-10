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

#### Scenario: 合法重建边界

- **WHEN** 首次组装、实际上下文压缩、rewind 重建 active view或 ToolSet hard rebase发生
- **THEN** 系统创建新的 prefix epoch并记录 `initial`、`compaction`、`rewind`或 `toolset_changed`原因；新 epoch内的后续请求重新遵守字节级稳定约束

#### Scenario: ToolSet 变化不得沿用旧 epoch

- **WHEN** 下一次模型请求的 ToolSetRef或工具 policy hash不同于当前 applied binding
- **THEN** 系统不得在旧 prefix epoch继续 seal或只追加工具切换文本，必须先执行 ToolSet hard rebase

### Requirement: 所有上下文 mutation 必须经唯一 owner 并保持 domain 分工

系统 SHALL 由每个 `SessionThread` 唯一、长生命周期的 RolloutCheckpointSaver/ContextStore owner处理所有会影响 canonical history、active view、source control state、ToolSet binding和 sealed assembly的变更。owner key 是 `(session_id, thread_id)`；Session 只保存 main-thread 路由、thread catalog与共享资源。该边界 MUST 区分 canonical append、source lifecycle decision、ToolSet switch和 compaction/rewind epoch rebuild；共享 owner与原子 transaction不得把这些 domain事实都改造成 CSM source。CSM SHALL仅管理需要 identity/revision/diff/tracking/reconciliation的 instruction、file和 runtime source，不得截获普通 canonical item、ToolSet或 compaction summary。

#### Scenario: 提交真实用户输入

- **WHEN** 可信 ingress接受用户文本或附件
- **THEN** acceptance/Turn owner构造 canonical append并由统一 ContextStore owner原子提交 user root；CSM不创建 source revision或 tracking registration

#### Scenario: 提交模型和工具协议事实

- **WHEN** 模型产生 assistant/reasoning/tool call或工具产生与 `tool_call_id`配对的 result
- **THEN** model stream/tool execution domain构造 canonical append并由同一 owner保持 item identity、origin Turn、协议配对和 ordinal；CSM不按文本去重或接管其生命周期

#### Scenario: 提交 source lifecycle 决策

- **WHEN** CSM观察到一个 instruction、tracked file或 runtime source需要新增 base、delta、恢复 item或 control state
- **THEN** CSM只返回结构化 lifecycle decision，由统一 owner在同一 transaction中提交 source item、revision、checkpoint state和 assembly selection

#### Scenario: mutation transaction 失败

- **WHEN** canonical append、source reconciliation、ToolSet switch、active-view rebuild、plan或 seal的任一步失败
- **THEN** owner不得部分推进 item、source committed revision、applied ToolSet revision或 prefix epoch，并返回具体 domain failure

### Requirement: ToolSet 变化必须在 model-call safe boundary hard rebase

系统 SHALL 将 ToolSetSnapshot/ToolSetRef保持为独立于 canonical message和 CSM source的权威事实。任何改变模型可见或可调用工具集合、schema、description、visibility、执行权限或确认 policy的有效变化都 SHALL产生 desired ToolSet revision；owner MUST在每次 model call前比较 desired与 applied revision，并在安全边界通过 hard rebase生效。hard rebase MUST封存新的 ToolSetSnapshot/Ref、创建 `epoch_reason=toolset_changed`的新 prefix epoch并重编译 root context、active canonical/source projection和 tools；不得向旧 context追加“工具已切换”之类的软通知，也不得修改 in-flight sealed assembly。

ToolSelectionStore/ToolService SHALL只拥有 workspace/agent级 desired selection及其控制面 revision；每个 SessionThread ContextStore SHALL拥有最后观察到的 desired revision、applied ToolSetRef/revision及其 prefix epoch。恢复或重放历史 assembly MUST使用已封存的 applied binding，不得用当前 desired selection反推历史工具集。

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

### Requirement: Root system 与 post-user source role 必须按位置确定

每个合法 prefix epoch的最顶层 root context SHALL至多投影为一个 `wire_role=system` source item；首次 assembly建立该 root，compaction、rewind或 ToolSet hard rebase只能在新 epoch重编译它。第一条真实用户消息之后追加或恢复的 CSM source item SHALL默认投影为 `wire_role=user`，不因其内容是完整 base、delta、rewind恢复、compaction物化或 hard rebase reconciliation结果而改变，也不得被吸收进新 root。wire role只能表达 Provider投影，不得改变 canonical source identity、scope或 Turn归属。

#### Scenario: 首次组装 root context

- **WHEN** Session/branch 在第一条真实用户输入前组装基础说明、初始 AGENTS 和 Skill metadata
- **THEN** 系统将这些初始贡献编译为一个最顶层 system wire item，并封存其 source provenance

#### Scenario: 用户消息后加载完整 Skill

- **WHEN** 模型在真实用户消息之后调用 `skill_load` 并读取完整 Skill activation
- **THEN** activation 作为新的 ambient source item追加到上下文尾部并投影为 user role，不前插或合并进 root system item

#### Scenario: rewind 或 compaction 完整恢复

- **WHEN** tracked source 在 rewind 后需要恢复当前完整 revision，或在 compaction 后物化为完整 revision
- **THEN** 完整恢复 item 仍位于用户消息之后并投影为 user role，不提升为中途 system item

#### Scenario: Provider 原生工具 role

- **WHEN** Provider 协议要求 tool call 或 tool result 使用其原生 role/item type
- **THEN** 系统保留该协议 role；post-user user-role 规则仅约束 CSM source item

#### Scenario: ToolSet hard rebase 重编译条件化 root

- **WHEN** ToolSet变化使一个仅在特定工具 policy下启用的初始 instruction source改变 included状态
- **THEN** assembly compiler只在新的 `toolset_changed` epoch重编译最顶层 root system item；既有 post-user source仍按原 canonical ordinal以 user role存在，不被提升或合并进 root

### Requirement: SkillCatalog 必须隐藏路径并分离 metadata 与 activation

系统 SHALL 从 bundled、`${BOXTEAM_HOME}/skills/` 和当前 workspace Skill 来源构造确定性的有效 catalog，并只向模型暴露唯一 Skill 名称及描述。workspace 同名项优先于 Gateway 全局项，Gateway 全局项优先于 bundled 项。模型不得看到或传入 Skill 的绝对路径、相对路径或 catalog locator。一个 `SKILL.md` 的 metadata 与 activation SHALL 使用独立 source identity 并分别生效；metadata 当前只包含 `name` 和 `description`。

#### Scenario: Gateway 全局 Skill 可用于所有工作区

- **WHEN** `${BOXTEAM_HOME}/skills/<skill>/SKILL.md` 提供有效名称和描述且 workspace 没有同名覆盖
- **THEN** 该 Skill 出现在所有工作区的有效 catalog 中，但具体 Session activation 和 CSM 状态仍由工作区 Saver/ContextStore 持久化

#### Scenario: 同名 Skill 按固定优先级解析

- **WHEN** workspace、Gateway 全局或 bundled 层存在同名 Skill
- **THEN** 系统按 `workspace > gateway-global > bundled` 选择唯一 entry，并保持内部 origin provenance，不向模型泄露路径

#### Scenario: metadata 与 activation 分别生效

- **WHEN** catalog 已向模型提供一个 Skill 的 name/description，但模型尚未调用 `skill_load`
- **THEN** metadata 可以存在于模型上下文，而 Skill activation 正文不得因此加载或生效

#### Scenario: 忽略其它 metadata 字段

- **WHEN** `SKILL.md` frontmatter 还包含 name/description 之外的字段
- **THEN** 当前 catalog、source hash contract 和模型可见 metadata 不读取或解释这些字段

### Requirement: `skill_load` 必须支持 snapshot、tracked 与 untrack

系统 SHALL 提供 `skill_load(name, mode)`，其中 mode 只能是 `snapshot`、`tracked` 或 `untrack`，缺省值为 `snapshot`。工具 SHALL 只接受模型可见名称，由软件解析内部 source identity/path；工具结果不得暴露内部路径或完整 Skill 正文。系统不得提供会立即删除既有 Skill 上下文的另一工具。

#### Scenario: 默认 snapshot 加载

- **WHEN** 模型调用 `skill_load(name)` 或显式使用 `mode="snapshot"`
- **THEN** 系统稳定读取一次当前 `SKILL.md`，追加一个完整不可变 activation item，随后不检查文件变化，也不在 rewind 移除后自动恢复

#### Scenario: tracked 加载并检测变化

- **WHEN** 模型调用 `skill_load(name, mode="tracked")`
- **THEN** 系统追加或复用当前完整 activation，保存 checkpoint-versioned tracking registration，并在每次模型请求前检查该内部 source 的 revision/hash

#### Scenario: tracked 文件没有变化

- **WHEN** tracked source 的当前 revision/hash 等于 active view 最新已提交可见 revision
- **THEN** 系统不追加 activation item、delta 或重复 selection

#### Scenario: tracked 文件发生变化

- **WHEN** tracked source 的稳定读取结果不同于 active view 最新已提交可见 revision
- **THEN** 系统计算到当前 revision 的一个 delta，以 user role追加到上下文尾部，并在 sealed assembly 提交后才推进 committed diff 基准

#### Scenario: untrack 停止后续追踪

- **WHEN** 模型调用 `skill_load(name, mode="untrack")` 且该 Skill 当前处于 tracked 状态
- **THEN** 系统将 registration 版本化为 untracked/frozen，不再检查文件或自动恢复，同时不删除、改写、重排或追加任何 Skill source 正文

#### Scenario: untrack 非 tracked Skill

- **WHEN** 模型对 snapshot 或不存在的 tracked registration 调用 `mode="untrack"`
- **THEN** 系统返回确定性的 `not_tracked` 结果，不伪造状态变化或 source item

#### Scenario: untrack 后重新 tracked

- **WHEN** 一个 untracked Skill 后续再次以 `mode="tracked"` 加载
- **THEN** 系统从 active view 最新可见 revision 恢复追踪；若没有可见 revision，则追加当前完整 revision

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

系统 SHALL 在 rewind 时从目标 checkpoint 恢复版本化 tracking registration，并按 snapshot、tracked、untrack 的语义重建 active view。compaction 需要物化 tracked source 时 SHALL 创建新的完整 revision和 prefix epoch，保留旧 lineage。所有位于真实用户消息之后的恢复/物化 source item必须使用 user role。

#### Scenario: snapshot 被 rewind 移除

- **WHEN** snapshot activation item 位于 rewind cutoff 之后
- **THEN** 该 item 从 active view 消失，系统不读取文件、不重新追加也不创建 tracking registration

#### Scenario: tracked revision 被 rewind 移除

- **WHEN** rewind 目标 checkpoint 仍包含 tracked registration，但其最新已注入 revision 不在重建后的 active view
- **THEN** 系统稳定读取并追加当前完整 revision，以 user role恢复且不创建普通 Turn

#### Scenario: untrack 状态参与 rewind

- **WHEN** rewind 目标 checkpoint 位于一次 untrack 之前或之后
- **THEN** 系统分别恢复该 checkpoint 当时的 tracked 或 untracked 状态，不使用进程当前内存状态覆盖 checkpoint 因果顺序

#### Scenario: compaction 物化 tracked source

- **WHEN** active source 为 `A(base) + A→B(delta) + B→C(delta)` 且 compaction 实际重建上下文
- **THEN** 系统创建以 C 为完整内容的新 revision/epoch，仅投影一个 post-user user-role 恢复 item，并保留旧链审计引用

#### Scenario: compaction 处理 snapshot 或 untracked 内容

- **WHEN** compaction 遇到 active view 中的 snapshot 或 untracked Skill 内容
- **THEN** 系统只能使用已提交 item/detail决定是否携带，不重新读取 `SKILL.md`，CSM 不执行自动恢复

### Requirement: 生产上下文来源必须完整登记并由正确 owner 接管

系统 SHALL 维护可验证的生产上下文来源迁移闭包，并为每类来源明确唯一 domain owner以及进入统一 ContextStore mutation owner的 intent。Agent 基础说明、运行时身份/路径、条件化团队规则、Todo/Filesystem/Skill catalog/AGENTS/压缩工具说明以及显式启用的 memory 属于 instruction/file source；Goal、委派与分支结果、非模拟用户的跨会话消息、团队动态事实、终端完成、模型重试控制和 checkpoint/runtime reminder属于 runtime event source。真实用户输入与附件、assistant/reasoning/tool协议事实通过 canonical append进入 owner；ToolSet通过 switch/hard-rebase进入 owner；compaction summary由 compaction owner产生。它们不得仅因参与同一模型请求而改造成 CSM source。生产请求不得包含来源闭包之外、没有 provenance的 system/user控制内容。

#### Scenario: 首次组装全部初始 instruction source

- **WHEN** Session 第一次组装模型上下文，并且一个或多个条件化 instruction source 当前启用
- **THEN** 系统把每个启用来源的 identity、revision、hash、included reason 和顺序登记到同一 sealed plan，再只在该首次边界将它们编译为唯一 root system item

#### Scenario: 内部事件触发新的模型执行

- **WHEN** Goal、委派/分支回报、非模拟用户的跨会话消息、团队更新、终端完成、retry 或 checkpoint reminder需要模型继续执行
- **THEN** 系统先以稳定事件 identity和幂等键提交 ambient/pending user-role source item，再通过独立 wakeup intent启动或继续 execution，不创建真实用户 acceptance、user root 或普通历史用户消息

#### Scenario: 生成 Session 的 seed prompt 明确声明语义

- **WHEN** 软件为新生成的 Session构造初始 prompt
- **THEN** producer 必须显式声明它是用户输入的可追溯派生 root还是内部 source；前者创建 canonical user root，后者走 runtime source 与 wakeup边界，系统不得根据文本内容或 wire role猜测

#### Scenario: 压缩派生请求与主上下文分离

- **WHEN** 系统为 compaction 创建派生摘要请求并将摘要结果带回主上下文
- **THEN** 派生请求拥有自己的 sealed assembly和 root/control items，摘要结果由 compaction owner保存为 canonical compaction summary；CSM只在同一压缩事务中物化需要恢复的 source，不重复登记摘要正文

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

- **WHEN** 已应用 AGENTS revision A 的 Session 在 before-model 检查中读取到 revision B
- **THEN** 系统登记 A→B delta并以 user role追加；原 root/base 不被原地替换，且不需要 watcher 或 `rg` 扫盘

#### Scenario: 通用 read 读取 SKILL.md

- **WHEN** 模型通过通用文件读取能力访问某个 `SKILL.md`
- **THEN** 该读取不建立 Skill activation/tracking；只有 `skill_load` 能注册 activation source

#### Scenario: 团队任务状态更新

- **WHEN** 团队成员更新任务状态并需要通知协调者 Session
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
- **THEN** 当前实现统一输出 user-role source item；Anthropic 官方部分模型的中途 system 能力只保留未启用 TODO，不参与当前 capability matrix 或运行时选择

### Requirement: Context lifecycle owner 必须精确为 SessionThread

系统 SHALL 将 ContextStore、CSM registration/revision/tracking state、active view、prefix epoch、ToolSet applied binding、sealed assembly 与 dispatch reference 绑定为同一 `(session_id, thread_id)` owner。Session 只持有权威 thread catalog、唯一 `main_thread_id`、导航和共享资源，不能作为 ContextStore/CSM 的隐式 owner；未指定 thread 的产品聊天/历史入口只能通过 catalog 明确解析 main thread 并返回实际 ID。LangGraph `checkpoint_ns` 只在已选 thread 内定位 framework graph/subgraph checkpoint，不能取代 product thread。

`RolloutCheckpointRuntime` MAY 作为 workspace singleton 组装组件，但 MUST 通过显式 `ThreadRuntimeBinding` 取得唯一 thread owner port，且不得成为第二个 JSONL/SQLite/context writer。相同 source locator/hash 在不同 thread 的 tracking、latest-visible-committed 基准、delta、rewind 恢复和 untrack state 必须独立；稳定前缀不得跨 thread 拼接或复用。

#### Scenario: 同一文件由两个 thread tracked

- **WHEN** main thread 与 delegated child thread 分别 tracked 同一个 AGENTS 或 Skill source，随后文件变化
- **THEN** CSM 分别相对于各自 latest-visible-committed revision 做 reconciliation，并只向对应 thread append/seal；一个 thread rewind、untrack、dispatch failure 或 compaction 不得改变另一个 thread 的 source state 或 prefix

#### Scenario: child result 进入 main thread

- **WHEN** durable delegated child thread 完成并需要通知 main thread
- **THEN** main-thread owner 以带 source `session_id/thread_id/item/execution` provenance 的 ambient/runtime item 提交通知；不得共享 ContextStore、直接引用 child active view，或把 child 内容创建为真实 user Turn root

#### Scenario: thread locator 必须由 catalog 解析

- **WHEN** main thread 或非主 durable thread 执行 source reconciliation、assembly seal、rewind或dispatch
- **THEN** owner通过 thread catalog/resolver取得受校验 locator；main thread解析为 `threads/{main_thread_id}`，其它 thread解析为按不可变UTC创建日期和`sha256(thread_id)[0:2]`分桶的路径，调用方不得自行拼接、扫盘或使用`checkpoint_ns`

#### Scenario: 附件正文不成为 CSM source detail

- **WHEN**真实用户输入携带已持久化附件，或 Provider/tool需要读取附件正文
- **THEN** ContextStore只提交逻辑attachment/variant reference及thread/item provenance；正文由workspace attachment catalog在校验capability、owner/view membership、hash和length后读取，CSM、assembly detail和模型可见内容都不包含物理blob locator
