## Why

`add-itemized-rollout-context` 已经定义 canonical item、ContextRequestPlan、assembly 和 rewind/compaction 的基础合同，但 AGENTS、Skill、团队状态与 checkpoint reminder 仍由不同 producer 直接拼接消息，既无法保证已提交上下文前缀的字节级稳定，也缺少统一的 source revision、追踪和恢复语义。

本 change 将所有上下文变更收敛到同一个 Saver/ContextStore mutation owner，同时只由 Context Source Manager（CSM）管理需要 revision、追踪和恢复的 source lifecycle；再以代码内装配的资源观察流水线和 统一资源寻址统一已知文件、Gateway 内部快照和内存状态的稳定快照、语义差异、模型可见来源与隐藏 locator，并把稳定前缀、资源激活边界、Skill 加载模式、ToolSet hard rebase 和 wire role 规则固化为可验证合同。模型工具的外部服务扩展继续使用 MCP；本 change 不建立通用资源插件宿主。

内部保存来源与版本还不足以让模型正确解释更新；本 change 同时补充统一的模型可见封装、更新解释提示、多来源批次聚合及 Skill 单次显式刷新语义，使模型能够区分用户任务与应用上下文更新，并理解同一来源新旧版本的生效关系。

## What Changes

- 新增统一的上下文 mutation 边界：真实用户消息、assistant/reasoning、tool call/result 通过 canonical append intent 提交；source lifecycle与compaction/rewind rebuild 使用各自语义明确的 intent；ToolSet使用完整typed ToolSetSnapshot作为prepare/seal输入，但全部由同一个 Saver/ContextStore owner 原子提交和 seal。
- 与 `add-itemized-rollout-context` 的实验 v2 字段收紧共同实施：CSM 和各 producer 只能提交/消费有 owner、类型和语义注释的 source lifecycle intent、contribution selection/replacement/source binding 与 registry ordinal；命名空间化、版本化 `extensions` 仅供保留/展示，不承担追踪、注入、排序或稳定前缀决策。直接删除旧自由 `metadata` flag/路径和兼容读取，不为现有实验数据建立第二套字段解释器。
- 该 owner 的最小生命周期单位是 `SessionThread` 而非产品 Session：Session 只保存唯一 main thread、child thread catalog 与共享资源；每个 thread 独立拥有 canonical context、CSM state、active view、prefix epoch、ToolSet applied binding 和 sealed assembly。LangGraph `checkpoint_ns` 仍仅为 thread 内子图 namespace，不得成为 CSM/ContextStore owner key。
- Session路径与导航改由itemized change的workspace SQLite catalog统一管理：物理node固定为`sessions/YYYY/MM/DD/{session_id}`，父子Session/Folder仅是catalog关系。所有新Session副作用准入先取workspace topology shared gate、再取本地Session gate并持久化lease；Session/Folder递归删除以同库batch record一次标记整棵逻辑子树deleting，catalog commit关闭新准入，之后逐Session关闭local fence、排空旧generation lease并定点隔离日期目录。CSM不能因导航移动重建owner、因删除时部分fence仍active而新建context，也不能扫盘补洞。
- 会话目录的乐观投影/持久命令队列由`add-itemized-rollout-context`定义。pending或仅accepted的导航意图不属于SessionThread canonical history、CSM source、ToolSet/ResourceActivation状态，也不能触发owner rehydrate或重新seal；生命周期准入只读workspace SQLite已经committed的catalog node/locator/generation。导航终态事件仅更新客户端展示，不作为绕过catalog/fence的执行许可。
- 将 main/child 的业务配置纳入上下文生命周期：main thread 承载 Session 的长期用户上下文和 Goal；durable child thread 用于同一 Session 内的大型任务分工，默认不启用 Goal，但拥有独立可恢复历史并允许用户直接对话。child 的原始上下文不复制回 main，只有带 parent/child/delegation provenance 的汇报进入目标 thread。
- 保证 durable ContextStore/CSM 状态可跨 Thread runtime unload 与重建保持不变：仅由 `add-itemized-rollout-context` 决定 Thread residency、30分钟阈值、准入、generation 与 cold read；本 change 定义 context owner 在获准 unload/rebuild 时保留的 source、tracking、prefix、ToolSet 与 assembly 事实。该卸载不产生 source item、prefix epoch 或上下文/Skill 到期语义。
- 固定SessionThread owner与外部附件正文的物理边界：Session node由workspace SQLite catalog解析到`sessions/YYYY/MM/DD/{session_id}`，main thread位于其`threads/{main_thread_id}`，其它durable thread直接位于`threads/YYYY/MM/DD/{thread_id}`；附件正文直接位于workspace `.boxteam/attachments/YYYY/MM/DD/{blob-id}`内容寻址store。CSM/ContextStore只持久化逻辑attachment reference、owner/view membership和provenance，不把任何物理locator注入模型上下文或source detail；逻辑导航移动不改变thread context路径或已提交前缀。
- 新增 `ContextSourceManager`（CSM）作为该 owner 下的 source lifecycle 子管理器，负责 AGENTS、Skill、Session内部团队角色/任务状态及其它动态 source 的 identity、revision、diff、追踪状态和 reconciliation；它不接管普通 canonical append，也不维护注入次数。
- 新增由 Gateway/Workspace 进程代码显式装配的资源观察流水线：内置文件监视、Gateway 受认证快照和权威内存状态适配由各自 owner 维护。Gateway Skill 已有专属 raw owner 发布认证快照、Workspace 校验并接受快照的链路；后续在此基础上补充按来源选择事件或轮询的策略，不恢复已回收的 generic `gateway_snapshot`、version token 或 memory-state 适配。可通过 typed port 在测试中注入替身，但不支持安装/热替换任意资源插件。唯一通用`LifetimeScope`持有进程内task、订阅句柄和client并负责异步释放；文件监视按完整watch语义共享订阅、引用计数，domain owner决定何时启动、替换或关闭scope。通用`EventChannelService`通过独立channel、背压和cursor分发轻量dirty/change/gap事件。`SourceReconciler`发布不可变来源revision，`ResourceDerivationGraph`按无环依赖图和语义diff/CAS发布不可变`ResourceSnapshot`；`SKILL.md`一源多facet、多层配置多源一有效资源，watch event不是内容或一致性权威。
- 模型请求正常路径不再读取、stat、枚举或扫描 AGENTS、Skill/config 文件。资源 provider 持续监视其已登记的有界资源；真实execution取得active slot时按itemized唯一合同冻结或恢复不可变`ResourceActivationPolicySnapshot`与`ExecutionResourceSnapshot`。默认所有resource kind复用Turn binding；按kind配置为`model_call`时，每次安全preparation建立引用该execution snapshot的`ModelCallResourceSnapshot`，只替换这些kind的binding并复用其它Turn binding，不执行请求期I/O。policy热更新只影响后续新用户Turn或独立internal execution，sealed request永不受中途变化影响。
- 新增 统一资源寻址：模型只看到 `boxteam://.../resources/...` 语义 URI，用来理解 workspace、Gateway 或 inline 来源（`builtin` 正名为 `inline`；`memory` 已确证不是 URI scope，不作为来源命名空间）；Registry 内部使用不可伪造的 `resource_id`，实际 owner 私有保存物理路径、内部 endpoint 或 credential ref（provider locator 举例中的 memory key 已随 remove-agent-memory 回收，不再存在）。URI 是 locator/provenance而不是授权凭据、资源 identity 或幂等键，历史 assembly 只读取当时封存的 snapshot，不按当前 URI 重新解析正文。
- 将“已提交上下文前缀字节级稳定”设为首要约束：同一 `prefix_epoch` 内的后续请求只能在已提交 wire context 后追加新 item，不得回写、合并、重排或重新序列化旧 item；来源/root 重建边界为首次组装、实际 compaction、rewind 和 ToolSet hard rebase；实际模型路由选择不同 Provider profile 时，另以 `provider_changed` 仅重投影冻结内容，不刷新来源或重编 root。
- Provider可见的少量直接工具与固定`invoke_extension_tool`信封构成唯一ToolSet；只有它们的Provider可见形状/策略有效变化才在model-call安全边界hard rebase。内层MCP/内置/自定义ExtensionToolCatalog的增删改与权限变化不制造ToolSetRef或prefix epoch，目录binding与调用目标独立封存、按调用时最新权限校验，旧tool call仍需真实配对收敛。
- MCP工具目录通过明确的McpCatalogOwner观察、验证和发布，再由McpToolGuidanceProducer生成受控CSM指引；目录与指引默认下一Turn一起生效，可按resource kind配置为下一安全model_call生效。不自动注入原始MCP prompts/resources/instructions；不在模型请求路径访问MCP server。将现有`invoke_custom_tool`与相关提示词、schema、展示和测试直接迁移为`invoke_extension_tool`，不留别名。
- Source owner以typed`root_placement=root_eligible|tail_only`声明根指令资格：普通同epoch变化无论full/delta都只在尾部追加`wire_role=user`消息，同批来源默认聚合为一条消息并保留配置为逐来源独立消息的能力，来源本身的身份与生命周期仍分别管理；首次组装及实际compaction、rewind、Provider ToolSet hard rebase或fork目标首次assembly的新epoch才允许把合格受信来源的完整有效状态合并为唯一顶层system root。默认外部MCP指引只能作`tail_only`数据，旧sealed bytes和canonical lineage不改写。
- 为 Anthropic 官方部分模型未来可能支持的中途 system item 仅保留 Provider capability TODO；当前运行时不得启用该分支，也不得因此改变通用 role 合同。
- 将一个 `SKILL.md` 拆为相互独立的 Skill metadata 与 Skill activation source；metadata 当前只读取 `name` 和 `description`。
- 新增仅按名称调用的 `skill_load(name, mode="snapshot" | "tracked" | "untrack")` 工具，默认 `snapshot`；模型不可读取或传入 Skill 路径。
- `snapshot` 从当前ResourceActivationSnapshot取得ResourceRegistry已稳定发布的 `SKILL.md` revision并追加一个不可变 activation item，之后不自动检查文件变化，也不在 rewind 移除后自动恢复；再次显式调用的刷新行为见下文补充方向。`StableSourceReader`、provider-owned可重建handle、双读一致性、固定大小/UTF-8边界和原始字节hash只属于Resource Reconciler，不在工具或模型请求路径运行；Gateway global正文经专属认证快照链路取得，Workspace 不以读取 Gateway 宿主路径代替该链路。
- `tracked` 由 CSM 只保存稳定 `resource_id`、source identity、catalog/provider binding revision、已应用revision/hash和追踪状态；私有provider handle/locator归ResourceRegistry与provider所有。Resource Reconciler异步发布变化，CSM只在配置的 `turn|model_call` 激活边界消费最新 `ResourceSnapshot`，把相对最新已提交且仍可见 revision 的 diff 追加到尾部，并在有效 registration 被 rewind 保留但最新注入移出 active view 时追加published完整revision。
- `untrack` 仅停止该 CSM registration 后续消费Registry revision与自动恢复，不停止共享monitor为其它consumer观察资源，也不删除、改写或立即移除已经进入上下文的item；本change不提供Skill即时移除工具。
- 多次尚未进入 sealed assembly 的 tracked 变化合并为一个从最新已提交可见 revision 到当前 revision 的 delta。
- SkillCatalog 按 **Workspace 本地 > Gateway 本地 > 网络来源 > 内置 inline** 选择同名 Skill；网络 Skill 校验成功后纳入目录，统一支持 `skill_load(name)`。网络来源仍由所属 Workspace 或 Gateway 获取和更新，名称选择统一处于网络层；同层支持显式来源优先级，同优先级仍同名则明确报冲突。内容更新保持稳定来源身份，目录优先级变化不自动改绑已有 tracked registration；Gateway 不写工作区 Session/CSM 状态。
- 将团队状态和其它内部通知迁移为 ambient/pending runtime source item；`wire_role=user` 只是 Provider 投影，不得创建真实用户 Turn root。team/member/task/coordinator状态只存在于单个Session内部并引用child thread，Goal只属于main thread。
- 将跨Session协作限定为经Gateway面向目标main thread的显式send/read/wait，并采用中心Gateway hub-and-spoke联邦：中心Gateway通过现有SSH `-L`主动连接各spoke，再在隧道内建立长期全双工WebSocket对等RPC channel；spoke可以在同一channel反向发起请求，中心最多中继一次`B → A → C`，不得继续多级转发。裸Session ID使用local/hub有界exact-ID discovery，显式federated URI按稳定`gateway_id`选路；中心验证真实origin并为target签发绑定origin/transit/path的短期grant，target重新验证grant和自己的最新policy后解析main thread。Gateway federation核心操作对已认证、已登记拓扑默认允许，限制规则与额外hardening默认关闭且可运行时原子修改；权限不进入模型上下文或ToolSet，每次discovery/send/read/wait/transit实际执行、分页或结果披露时读取最新policy revision，撤权停止尚未接受的操作或后续披露，已durable acceptance的send不回滚。身份认证、grant完整性、audience/path、first-use防重放和业务幂等属于不可关闭的协议正确性。持久`connection_id`只作本地配置身份，单次`channel_instance_id`/`connection_epoch`/route lease为瞬时状态，均不进入Session link、`GlobalThreadAddress`或业务幂等键。source outbox、target inbox继续承担持久通信与崩溃恢复，中心只保存peer/policy/replay registry/审计等控制面状态，不保存消息正文或替代端点账本。分页read使用AEAD opaque一致性快照且逐页授权，read/wait以稳定operation invocation和有界observation record恢复同一snapshot/baseline/result；模型侧send删除`simulate_user`并返回durable acceptance/execution binding，默认60秒/最大300秒的`wait_for_session`消除accepted/queued假idle。跨Session通信不得保存或同步team/member/task/role/Goal状态。
- 旧开发期跨Session team/member/task/coordinator与delegated Session数据按用户授权直接清理；生产只接受同Session child-thread协作和当前格式，不新增旧board迁移、物化或freeze/detach兼容入口。新格式创建、同事务发布、同版本重启和删除恢复仍须完整验证。
- Thread residency 状态投影、deadline、blocker 与可注入单调Clock由 `add-itemized-rollout-context` 唯一规定；本 change 不复制其调度合同，也不把runtime观察写入上下文。
- 建立现有生产上下文来源的迁移闭包：覆盖 Agent 配置说明、运行时身份、Todo/Filesystem/Skill/AGENTS/压缩 middleware、Goal/委派/跨会话/团队/终端/retry/checkpoint 事件，并逐项指定 CSM、ToolSet、compaction 或 canonical Turn owner（原闭包内的 Memory middleware 已随 change `remove-agent-memory` 整体移除，不再纳入）。
- **BREAKING** 删除 `PromptReplayCaptureMiddleware`、捕获标签和 middleware 间 prompt diff 链路；不保留诊断兼容模式，所有生产 instruction/runtime producer 必须在 assembly seal 前显式登记。
- **BREAKING** 废弃 `ItemizedContextProjectionMiddleware` 从已组装 LangChain request 反向捕获 prompt/tool/context 的实现。框架若必须使用 model-call middleware 钩子，则将该接入点改造成无状态 sealed-assembly dispatch bridge；框架不需要时直接由 Provider dispatch adapter消费 sealed assembly。
- **BREAKING** 删除生产路径中由 middleware 通过通用 read 工具加载 Skill、直接追加内部 `HumanMessage`，以及通过可变有效期或计数改写 active context 的实现。
- **BREAKING** 删除模型可见的 `/.boxteam/skills`、`/.boxteam/bundled-skills`、`.boxteam/.../SKILL.md` 路径提示及通用 `read_file` Skill 白名单；`skill_load` 仍只接受名称，软件从冻结的 SkillCatalogSnapshot 解析 exact resource handle，并在结果/context provenance 中返回安全虚拟 URI。
- **BREAKING** 迁移 config、AGENTS、Skill 和 workspace 文件事件消费者后，删除各自独立的 watcher loop、request-time reader/enumerator 与兼容 adapter；现有 Job event bus 改为建立在通用多 channel EventChannelService 上的业务 adapter，资源事件不得继续进入 job 专属队列或与其争用背压。
- **BREAKING** 收敛旧`TurnExecutionScope`内的通用cleanup、各runtime手写stop/close与资源平台task树为一个进程内释放合同；取消信号只传递取消意图，`LifetimeScope`只关闭其持有的运行期句柄。旧`ResourceManager`不再猜测工具参数、保存`cleanup_policy`或凭内存stopper宣称外部资源已停止；跨Turn外部资源由实际provider/domain owner执行并核实操作，持久操作lease只负责占用与恢复。会话资源列表/控制不得形成第二事实源。
- Node调试进程的跨Turn占用由debug owner在spawn前按唯一process-instance建立持久claim，不能由短期tool/Web operation lease替代；重启时以可验证进程代际证据核实，PID/端口复用或证据缺失不得被误认作成功停止。

- **技能目录读取与激活分离（2026-10-05 修订）**：允许通用文件工具按统一寻址的技能包目录/单文件能力读取内容，读取不产生来源登记、activation或tracking。skill_load继续是冻结来源激活/追踪的唯一入口；文件访问消费合同归独立add-unified-resource-file-tools。

### CSM 模型可见协议与 Skill 刷新补充方向

以下记录本轮确定的需求，并同步到本 change 的 specs 与 tasks；后续仍可继续增补。具体标签语法、配置键及实现接口由设计与实施任务收敛，不在 proposal 固定。新增行为均按未完成任务跟踪，不表示现有实现或历史验收已覆盖。

1. **统一来源封装。** 首次激活、后续增量、完整替换和恢复均使用统一的模型可见来源结构，system root 中的来源与 user 更新中的来源沿用一致的来源表达。每个来源块应明确稳定身份、名称、类型、操作含义、当前版本及适用的前一版本，区分完整正文与 unified diff，并说明追踪模式和原有信任资格。物理路径、内部 locator 与凭据不得进入消息；统一包装不提升来源的指令资格。
2. **统一告知模型如何解释更新。** 提供集中的 CSM 使用提示，说明消息来自应用上下文管理器、与真实用户任务的区别、来源与版本的对应关系，以及 unified diff 的上下文行、删除行和新增行语义。完整替换使同一来源的旧版本失效，增量删除的旧内容不再生效；更新不撤销其它来源，也不改变既有指令优先级。不能只发送裸正文或 diff，让模型自行推断业务含义。
3. **多来源默认合并为一条 user 消息，并可配置切换。** 类似 system root 的来源聚合，同一模型调用待追加的 CSM 来源批次默认合成一条 `user` 消息，内部按确定性顺序放置各来源块，分别保留身份、版本、操作及提交追踪。保留不合并、逐来源投影为独立 `user` 消息的配置参数，便于快速切换和对比测试；参数归入 Workspace 上下文配置域，具体名称和层级后续细化。两种模式共用同一来源封装与生命周期链路，只改变消息分组。聚合在 seal 前完成，不回头合并已封存消息，不混入真实用户输入或工具协议消息，也不由 Provider adapter 临时合并；配置切换仅作用于后续尚未封存批次。
4. **Skill 一次显式调用即可刷新。** 再次调用 `skill_load(name)` 应可加载本次激活边界冻结的已发布版本，不要求先 `untrack` 或卸载，也不另增含义重复的 reload 参数。同来源同版本仍有效时复用正文；同来源新版本通过一次调用完成刷新，并向模型明确新版取代旧版。snapshot 与 tracked 是互斥加载模式，每次显式加载以请求 mode 为准：省略 mode 等于 snapshot，停止该名称已有追踪；tracked 则建立或保持追踪。同版本正文复用不能跳过模式切换，回执须反映实际追踪状态。同名但来源身份发生变化时应明确表达来源切换及旧、新来源的生效关系，不能只按名称混同；加载与追踪状态变更共同成功，失败保留原有效状态，不产生半完成刷新。“最新版”指本次配置激活边界可取得的冻结已发布版本，不承诺工具调用瞬间读取磁盘，也不绕过现有激活策略。
5. **区分历史保留与当前生效。** 新版生效通过追加更新及明确的替代关系表达，旧 sealed 消息仍保留供历史复现，不继续作为该来源的当前版本解释。`untrack` 只停止后续追踪与自动恢复，不卸载、不撤销已加载正文；本轮不增加 Skill unload。snapshot 依靠有效上下文保留，被 rewind 移除或在压缩中未被保留时不自动恢复；真正卸载或撤销另行讨论。

### 来源更新能力与正文入口补充方向

- **核清并补齐 tracked 更新链路。** 持续更新的来源应有明确 owner、版本发布入口和消费激活边界，并能校验相应更新能力已经接线；不能仅凭登记 tracked 就宣称更新可用，也不强制每个来源拥有 watcher 或独立回调。R01 已有真实配置 reload 经 runtime generation 更新并追加来源内容的链路与集成测试，后续复用并核清覆盖范围，不将它笼统视为未实现；重点补齐 R01／R02 的实际缺口，区分 generation 重建与 model-call 激活能力。一次性内容采用 snapshot 语义。
- **明确观察结果。** `observe()` 应区分未变化、恢复基准、来源未跟踪和接受新观察等结果，不再用同一个布尔值掩盖处理差异；必须跟踪的入口遇到状态不符应明确报错。观察结果、待提交状态与封存成功分开表达，不能将接受观察一律解释为已排队或已生效，回到已提交版本时仍可取消待提交增量。
- **以 item 区分操作与追踪状态。** 逻辑 item 仍是来源管理、选择和封存追踪的基础单位，消息合并仅是模型投影分组，各 item 的身份、版本、顺序和引用继续保留，不在聚合消息上制造一份统一 tracking 状态。命名方向为：保留外部 `skill_load.mode`，`SourceObservation.tracking_mode` 改为 `requested_mode`；运行态及持久控制态保留 `tracking_status`；来源 item／绑定的封存字段改为 `source_tracking_at_seal`，派生属性与回执 `tracked` 保留。canonical item 的“不适用 tracking”与来源 item 的“未跟踪”应在类型合同上区分，不只做字符串替换；具体结构随后细化。
- **统一正文与默认值的权威入口。** R02 分离可覆盖说明和软件生成的身份事实，清理具体项目示例，对外统一发布有效正文与版本；说明覆盖不能篡改身份事实，首选／fallback 配置与本次实际模型身份不混同。R01 默认提示词保留一个权威入口，清理 JSONC 与 Python 的重复默认及兜底，同时核清缺省输入的既有行为。
- **增加可选外部提示词文件入口。** 字符串与文件入口的选择、路径解析和覆盖规则随后确定；由来源 owner 负责读取、发布有效正文与版本，文件正文变化应推进来源 revision，显式配置的文件读取失败须报错。模型请求路径只消费已发布的冻结版本，不临时读盘，也不维护第二套来源状态。

### 来源能力与快照更新策略分离

- 将地址解析、快照获取、更新触发、对账发布与上下文激活的职责分开，复用现有 owner 与资源流水线，不为每个层次另建管理框架。外部地址由来源 owner 用于获取，模型可见资源身份沿用既有统一寻址合同，不把外部 URL、私有 locator 或凭据透传为模型可见身份。特殊获取协议通过已登记能力接入，不把来源限定为本地文件、Gateway 和 HTTP 三种，也不由地址直接决定更新策略。
- 配置以声明方式选择已实现的解析／获取能力、触发方式及参数，并引用其绑定关系。支持事件、周期轮询和手动触发；默认选择一种，确有需要时可显式组合事件与低频对账，全部汇入同一对账入口，不维护第二套快照或 CSM 状态。配置不承载任意代码或回调，未知协议、未登记能力及不支持的组合应明确报错。
- 本地文件监听、Gateway 发布后通知 Workspace、HTTP 链接周期轮询作为可覆盖的默认示例，不作为按 URI 类型写死的分派规则。相同来源可快速切换策略，不同来源也可复用同一种获取或触发能力；网络获取可由事件触发，而不必只能轮询。轮询允许分钟、小时等可配置间隔，具体配置键和组合语法随后细化。
- 保留 Gateway 先发布快照再通知、Workspace 获取并校验 accepted 快照的链路；Gateway 本地如何发现变化与 Workspace 如何获知版本分别配置。Workspace 私有来源由 Workspace 获取，共享 Gateway 来源由 Gateway 统一获取并分发；增加显式登记的 HTTP(S) Skill 链接能力，不扩展为任意网络资源自动发现或执行。
- 快照更新不等于指令立即生效：任何触发方式均复用对账、快照发布、语义派生与激活链路，继续遵循激活边界、snapshot／tracked 语义及统一封存规则。首次订阅、重连和事件缺口主动对账，不静默降级；观察策略、轮询周期与上下文激活策略分别配置。

## Capabilities

### New Capabilities

- `context-injection-lifecycle`: 定义稳定前缀、CSM source revision/追踪生命周期、Skill catalog 与 `skill_load`、wire role、rewind/compaction 恢复和 sealed assembly 投影合同。

### Modified Capabilities

本 change 通过显式集成合同复用 `add-itemized-rollout-context` 已规划的能力，不直接修改或复制其 requirement。

## Impact

- 影响 `app/agents/agent_factory.py`、`deep_agent_stack.py`、`middleware_prompts.py`、`skill_runtime.py`、压缩 middleware、内部 structured prompt producer；删除 `PromptReplayCaptureMiddleware`，并删除或重构 `ItemizedContextProjectionMiddleware` 为无状态 sealed-assembly dispatch bridge（原列出的 memory middleware 已随 change `remove-agent-memory` 删除，不属本 change 影响面）。
- 影响 checkpoint/context owner、source reconciliation、assembly manifest/detail store 和 Provider projector：owner 接受 canonical append、source lifecycle与epoch rebuild三类mutation intent及完整ToolSetSnapshot输入；applied工具事实仅由成功sealed assembly binding确立；dispatch 只能消费 Saver-issued sealed assembly reference，不能从 framework request反向生成 contribution、ToolSetSnapshot或 assembly。
- 影响 SessionThread catalog、thread-qualified storage/checkpoint config 和 GraphBinding 重建：CSM source identity、tracking registration、ToolSet applied revision 与 sealed dispatch reference 必须绑定 `(session_id, thread_id)`；Session main-thread 默认路由不能被内部 producer 当作跨 thread fallback。
- 影响由 `add-itemized-rollout-context` 定义的 ThreadRuntime residency、lazy rehydrate 和 callback fencing：logical ContextStore/CSM owner必须可从持久状态重建且任意时刻只有一个可写runtime generation；cold read与 idle policy复用其唯一合同。
- 影响 attachment ingress、canonical append、Provider projection 与清理流程：需要 workspace attachment catalog、按内容去重、session/thread/item reference、capability 校验和引用感知 GC；CSM、history 和模型工具不得扫描 `.boxteam/attachments` 或暴露其 locator。
- 影响 ToolSelectionStore、ToolService、执行 step/Agent 生命周期和 ToolSet registry：需要以控制面 desired selection/revision 对照 SessionThread sealed assembly binding 的唯一 applied ToolSet revision，在每次新 model call 前冻结完整 ToolSetSnapshot并检测变化，并以 hard rebase替代“运行中 Job 永久沿用旧 Agent/工具集”的隐式行为。
- 影响 Goal、subagent/session generation、跨会话消息、团队、终端 steering、execution retry 和 checkpoint reminder 的派发方式；Goal只绑定main thread，team状态迁入Session内部child-thread ledger，跨Sessionsend只投递目标main thread；这些内部事件需要独立于真实用户 acceptance/Turn root 的 execution wakeup。
- 影响`send_message_to_session`、`read_context`、等待/监控工具及Session link解析：跨workspace/server操作不materialize目标runtime或建立共享协作状态，目标resolved main-thread identity必须进入审计和幂等记录。
- 影响Gateway federation transport、配置与权限控制：新增SSH隧道内的长期全双工WebSocket对等RPC channel、hub唯一中继、origin-preserving transit grant、channel重连/背压/多路复用，以及`permissions.federation`默认允许且运行时热发布的policy snapshot；现有持久`connection_id`不改成socket实例身份。
- 影响 SkillCatalog 的四层名称解析、网络 Skill 登记与 owner 获取更新策略，以及 `${BOXTEAM_HOME}/skills/`；Gateway 仍不得读写工作区 `.boxteam/` Session 数据。
- 影响 config watcher、workspace file watcher、Job event bus及其进程装配：以代码内显式接线替代贡献定义registry和插件装载，新增SourceReconciler、语义资源依赖DAG、通用`LifetimeScope`、按channel隔离的事件服务、ResourceRegistry、activation coordinator和启动期initial reconcile/readiness gate；来源/语义identity与revision独立，文件监视按完整选项语义去重并按订阅引用计数。旧`ResourceManager`收敛为外部资源操作lease账本，实际资源状态和停止结果只由其domain owner提供；不把release、watch共享、generation发布或业务停止策略塞入单个Manager。目标模块归属和各目录边界见design 5.3。
- 影响 workspace backend、文件工具与模型提示：新增 `boxteam://` URI parser/resolver、typed capability dispatch 和安全 display URI projection，删除旧 `/.boxteam/...` Skill 虚拟挂载对模型的暴露及通用读取激活入口。
- 需要增加 CSM 追踪控制状态、resource descriptor/snapshot/activation snapshot、URI/resource provenance、source revision/detail、稳定前缀 epoch/reason/hash/length、desired/applied ToolSet revision、Skill metadata/activation provenance 及 `skill_load` 工具 schema。
- 需要更新 Python 单元/集成测试、真实 Provider request projection、snapshot/tracked/untrack、rewind、compaction、restart 场景；跨change Web验收统一写入`tests/e2e/clients/web/test_basic_chat_tool_loop.py`，保留基础两轮item/time/order断言并增加main/child、cold runtime和跨workspace/session协作，不创建第二套E2E owner。
- 本 change 已具备实施计划，按 tasks 中边界明确的独立切片推进实现与验收；持久化继续由单一 Saver/ContextStore owner 负责，不建立第二个 SQLite/JSONL writer。团队／协作专项按已登记延期范围处理，不阻塞其它独立切片。
