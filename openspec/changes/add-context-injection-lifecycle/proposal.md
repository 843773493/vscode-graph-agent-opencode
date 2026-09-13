## Why

`add-itemized-rollout-context` 已经定义 canonical item、ContextRequestPlan、assembly 和 rewind/compaction 的基础合同，但 AGENTS、Skill、团队状态与 checkpoint reminder 仍由不同 producer 直接拼接消息，既无法保证已提交上下文前缀的字节级稳定，也缺少统一的 source revision、追踪和恢复语义。

本 change 将所有上下文变更收敛到同一个 Saver/ContextStore mutation owner，同时只由 Context Source Manager（CSM）管理需要 revision、追踪和恢复的 source lifecycle；再以插件化 Resource Observation Platform 和 Virtual Resource Namespace（VRN）统一文件、网络、内存等资源的发现、稳定快照、语义差异、模型可见来源与隐藏 locator，并把稳定前缀、资源激活边界、Skill 加载模式、ToolSet hard rebase 和 wire role 规则固化为可验证合同。

## What Changes

- 新增统一的上下文 mutation 边界：真实用户消息、assistant/reasoning、tool call/result 通过 canonical append intent 提交；source lifecycle、ToolSet switch、compaction/rewind rebuild 使用各自语义明确的 intent，但全部由同一个 Saver/ContextStore owner 原子提交和 seal。
- 该 owner 的最小生命周期单位是 `SessionThread` 而非产品 Session：Session 只保存唯一 main thread、child thread catalog 与共享资源；每个 thread 独立拥有 canonical context、CSM state、active view、prefix epoch、ToolSet applied binding 和 sealed assembly。LangGraph `checkpoint_ns` 仍仅为 thread 内子图 namespace，不得成为 CSM/ContextStore owner key。
- Session路径与导航改由itemized change的workspace SQLite catalog统一管理：物理node固定为`sessions/YYYY/MM/DD/{session_id}`，父子Session/Folder仅是catalog关系。所有新Session副作用准入先取workspace topology shared gate、再取本地Session gate并持久化lease；Session/Folder递归删除以同库batch record一次标记整棵逻辑子树deleting，catalog commit关闭新准入，之后逐Session关闭local fence、排空旧generation lease并定点隔离日期目录。CSM不能因导航移动重建owner、因删除时部分fence仍active而新建context，也不能扫盘补洞。
- 会话目录的乐观投影/持久命令队列由`add-itemized-rollout-context`定义。pending或仅accepted的导航意图不属于SessionThread canonical history、CSM source、ToolSet/ResourceActivation状态，也不能触发owner rehydrate或重新seal；生命周期准入只读workspace SQLite已经committed的catalog node/locator/generation。导航终态事件仅更新客户端展示，不作为绕过catalog/fence的执行许可。
- 将 main/child 的业务配置纳入上下文生命周期：main thread 承载 Session 的长期用户上下文和 Goal；durable child thread 用于同一 Session 内的大型任务分工，默认不启用 Goal，但拥有独立可恢复历史并允许用户直接对话。child 的原始上下文不复制回 main，只有带 parent/child/delegation provenance 的汇报进入目标 thread。
- 将 durable ContextStore/CSM 状态与 resident Agent runtime 分离：child thread 默认在无 active/pending execution、未收敛 model/tool/mutation 和 runtime lease且连续30分钟无活动后卸载可重建进程资源；cold history读取不恢复runtime，下一次model execution在准入时按原ThreadRuntimeBinding延迟重建。该卸载不产生source item、prefix epoch或上下文/Skill到期语义。
- 固定SessionThread owner与外部附件正文的物理边界：Session node由workspace SQLite catalog解析到`sessions/YYYY/MM/DD/{session_id}`，main thread位于其`threads/{main_thread_id}`，其它durable thread直接位于`threads/YYYY/MM/DD/{thread_id}`；附件正文直接位于workspace `.boxteam/attachments/YYYY/MM/DD/{blob-id}`内容寻址store。CSM/ContextStore只持久化逻辑attachment reference、owner/view membership和provenance，不把任何物理locator注入模型上下文或source detail；逻辑导航移动不改变thread context路径或已提交前缀。
- 新增 `ContextSourceManager`（CSM）作为该 owner 下的 source lifecycle 子管理器，负责 AGENTS、Skill、Session内部团队角色/任务状态及其它动态 source 的 identity、revision、diff、追踪状态和 reconciliation；它不接管普通 canonical append，也不维护注入次数。
- 新增插件化 Resource Observation Platform：`ResourceContributionRegistry`只注册文件、网络、内存及后续provider的monitor/snapshot/loader/reaction定义；`ResourceTaskSupervisor`以树形生命周期管理订阅和worker，通用`EventChannelService`通过独立channel、背压和cursor分发轻量dirty/change/gap事件。`SourceReconciler`发布不可变来源revision，`ResourceDerivationGraph`按无环依赖图和语义diff/CAS发布不可变`ResourceSnapshot`；`SKILL.md`一源多facet、多层配置多源一有效资源，watch event不是内容或一致性权威。
- 模型请求正常路径不再读取、stat、枚举或扫描 AGENTS、Skill/config 文件。资源 provider 持续监视其已登记的有界资源；Turn取得active execution slot时先冻结不可变`ResourceActivationPolicySnapshot`与`TurnResourceSnapshot`。默认所有resource kind复用Turn binding；按kind配置为`model_call`时，每次安全preparation建立引用该Turn snapshot的`ModelCallResourceSnapshot`，只替换这些kind的binding并复用其它Turn binding，不执行请求期I/O。policy热更新只影响后续Turn，sealed request永不受中途变化影响。
- 新增 Virtual Resource Namespace（VRN）：模型只看到 `boxteam://.../resources/...` 语义 URI，用来理解 workspace、Gateway、builtin、memory 或 plugin 来源；Registry 内部使用不可伪造的 `resource_id`，provider 私有保存物理路径、网络 endpoint、credential ref 或 memory key。URI 是 locator/provenance而不是授权凭据、资源 identity 或幂等键，历史 assembly 只读取当时封存的 snapshot，不按当前 URI 重新解析正文。
- 将“已提交上下文前缀字节级稳定”设为首要约束：同一 `prefix_epoch` 内的后续请求只能在已提交 wire context 后追加新 item，不得回写、合并、重排或重新序列化旧 item；合法 epoch 边界只有首次组装、实际 compaction、rewind 和 ToolSet hard rebase。
- 将所有有效 ToolSet 变化定义为 hard rebase：当前 in-flight sealed model call 保持不可变，desired revision 在下一个 model-call safe boundary 生效，先真实收敛旧工具调用，再封存新 ToolSetSnapshot/Ref、创建 `epoch_reason=toolset_changed` 的新 prefix epoch 并重建 root/messages/tools 投影；禁止用 user-role 文本模拟软切换。
- 首次组装只生成一个最顶层 `wire_role=system` root item；第一条真实用户消息之后追加的 CSM 完整内容、delta 及 rewind/compaction 恢复内容当前统一使用 `wire_role=user`。
- 为 Anthropic 官方部分模型未来可能支持的中途 system item 仅保留 Provider capability TODO；当前运行时不得启用该分支，也不得因此改变通用 role 合同。
- 将一个 `SKILL.md` 拆为相互独立的 Skill metadata 与 Skill activation source；metadata 当前只读取 `name` 和 `description`。
- 新增仅按名称调用的 `skill_load(name, mode="snapshot" | "tracked" | "untrack")` 工具，默认 `snapshot`；模型不可读取或传入 Skill 路径。
- `snapshot` 从当前ResourceActivationSnapshot取得ResourceRegistry已稳定发布的 `SKILL.md` revision并追加一个不可变 activation item，之后不检查文件变化，也不在 rewind 移除后自动恢复。`StableSourceReader`、provider-owned可重建handle、双读一致性、固定大小/UTF-8边界和原始字节hash只属于Resource Reconciler，不在工具或模型请求路径运行；Gateway global正文由Gateway以受认证内部snapshot提供，物理路径不跨边界。
- `tracked` 由 CSM 只保存稳定 `resource_id`、source identity、catalog/provider binding revision、已应用revision/hash和追踪状态；私有provider handle/locator归ResourceRegistry与provider所有。Resource Reconciler异步发布变化，CSM只在配置的 `turn|model_call` 激活边界消费最新 `ResourceSnapshot`，把相对最新已提交且仍可见 revision 的 diff 追加到尾部，并在有效 registration 被 rewind 保留但最新注入移出 active view 时追加published完整revision。
- `untrack` 仅停止该 CSM registration 后续消费Registry revision与自动恢复，不停止共享monitor为其它consumer观察资源，也不删除、改写或立即移除已经进入上下文的item；本change不提供Skill即时移除工具。
- 多次尚未进入 sealed assembly 的 tracked 变化合并为一个从最新已提交可见 revision 到当前 revision 的 delta。
- 增加 `${BOXTEAM_HOME}/skills/` 的 Gateway 全局 Skill catalog，并与 bundled、workspace Skill 形成确定性名称解析；Gateway 不写工作区 Session/CSM 状态。
- 将团队状态和其它内部通知迁移为 ambient/pending runtime source item；`wire_role=user` 只是 Provider 投影，不得创建真实用户 Turn root。team/member/task/coordinator状态只存在于单个Session内部并引用child thread，Goal只属于main thread。
- 将跨Session协作限定为经Gateway面向目标main thread的显式send/read/wait，并采用中心Gateway hub-and-spoke联邦：中心Gateway通过现有SSH `-L`主动连接各spoke，再在隧道内建立长期全双工WebSocket对等RPC channel；spoke可以在同一channel反向发起请求，中心最多中继一次`B → A → C`，不得继续多级转发。裸Session ID使用local/hub有界exact-ID discovery，显式federated URI按稳定`gateway_id`选路；中心验证真实origin并为target签发绑定origin/transit/path的短期grant，target重新验证grant和自己的最新policy后解析main thread。Gateway federation核心操作对已认证、已登记拓扑默认允许，限制规则与额外hardening默认关闭且可运行时原子修改；权限不进入模型上下文或ToolSet，每次discovery/send/read/wait/transit实际执行、分页或结果披露时读取最新policy revision，撤权停止尚未接受的操作或后续披露，已durable acceptance的send不回滚。身份认证、grant完整性、audience/path、first-use防重放和业务幂等属于不可关闭的协议正确性。持久`connection_id`只作本地配置身份，单次`channel_instance_id`/`connection_epoch`/route lease为瞬时状态，均不进入Session link、`GlobalThreadAddress`或业务幂等键。source outbox、target inbox继续承担持久通信与崩溃恢复，中心只保存peer/policy/replay registry/审计等控制面状态，不保存消息正文或替代端点账本。分页read使用AEAD opaque一致性快照且逐页授权，read/wait以稳定operation invocation和有界observation record恢复同一snapshot/baseline/result；模型侧send删除`simulate_user`并返回durable acceptance/execution binding，默认60秒/最大300秒的`wait_for_session`消除accepted/queued假idle。跨Session通信不得保存或同步team/member/task/role/Goal状态。
- 旧跨Sessionteam board必须显式选择通过migration-only `materialize_thread_copy`从终态/quiesce的source main生成target-local child，或freeze/detach；该内部原语只复用copy mapping/校验引擎，不得创建新Session/main、自行发布child或改变公开`full_rollout_copy`语义。在创建任何child staging前先持久化冻结全部target locator/artifact manifest的preparing migration record，所有child再进入不可见staging，最后由coordinator catalog/board与该record的本地事务一次发布，不假设跨thread/workspace事务，不得形成无journal orphan、空child、部分board或外部Session与本地child混合member。
- resident runtime提供只读residency/deadline/blocker投影并使用可注入单调Clock；测试保持真实30分钟阈值，用fake clock验证临界点，不把runtime观察写入上下文。
- 建立现有生产上下文来源的迁移闭包：覆盖 Agent 配置说明、运行时身份、Todo/Filesystem/Skill/AGENTS/压缩/Memory middleware、Goal/委派/跨会话/团队/终端/retry/checkpoint 事件，并逐项指定 CSM、ToolSet、compaction 或 canonical Turn owner。
- **BREAKING** 删除 `PromptReplayCaptureMiddleware`、捕获标签和 middleware 间 prompt diff 链路；不保留诊断兼容模式，所有生产 instruction/runtime producer 必须在 assembly seal 前显式登记。
- **BREAKING** 废弃 `ItemizedContextProjectionMiddleware` 从已组装 LangChain request 反向捕获 prompt/tool/context 的实现。框架若必须使用 model-call middleware 钩子，则将该接入点改造成无状态 sealed-assembly dispatch bridge；框架不需要时直接由 Provider dispatch adapter消费 sealed assembly。
- **BREAKING** 删除生产路径中由 middleware 通过通用 read 工具加载 Skill、直接追加内部 `HumanMessage`，以及通过可变有效期或计数改写 active context 的实现。
- **BREAKING** 删除模型可见的 `/.boxteam/skills`、`/.boxteam/bundled-skills`、`.boxteam/.../SKILL.md` 路径提示及通用 `read_file` Skill 白名单；`skill_load` 仍只接受名称，软件从冻结的 SkillCatalogSnapshot 解析 exact resource handle，并在结果/context provenance 中返回安全虚拟 URI。
- **BREAKING** 迁移 config、AGENTS、Skill 和 workspace 文件事件消费者后，删除各自独立的 watcher loop、request-time reader/enumerator 与兼容 adapter；现有 Job event bus 改为建立在通用多 channel EventChannelService 上的业务 adapter，资源事件不得继续进入 job 专属队列或与其争用背压。

## Capabilities

### New Capabilities

- `context-injection-lifecycle`: 定义稳定前缀、CSM source revision/追踪生命周期、Skill catalog 与 `skill_load`、wire role、rewind/compaction 恢复和 sealed assembly 投影合同。

### Modified Capabilities

本 change 通过显式集成合同复用 `add-itemized-rollout-context` 已规划的能力，不直接修改或复制其 requirement。

## Impact

- 影响 `app/agents/agent_factory.py`、`deep_agent_stack.py`、`middleware_prompts.py`、`skill_runtime.py`、压缩与 memory middleware、内部 structured prompt producer；删除 `PromptReplayCaptureMiddleware`，并删除或重构 `ItemizedContextProjectionMiddleware` 为无状态 sealed-assembly dispatch bridge。
- 影响 checkpoint/context owner、source reconciliation、assembly manifest/detail store 和 Provider projector：owner 需要接受 canonical append、source lifecycle、ToolSet switch 与 epoch rebuild 四类 mutation intent；dispatch 只能消费 Saver-issued sealed assembly reference，不能从 framework request反向生成 contribution、ToolSetSnapshot或 assembly。
- 影响 SessionThread catalog、thread-qualified storage/checkpoint config 和 GraphBinding 重建：CSM source identity、tracking registration、ToolSet applied revision 与 sealed dispatch reference 必须绑定 `(session_id, thread_id)`；Session main-thread 默认路由不能被内部 producer 当作跨 thread fallback。
- 影响 ThreadRuntime residency、lazy rehydrate和callback fencing：logical ContextStore/CSM owner必须可从持久状态重建且任意时刻只有一个可写runtime generation；child默认30分钟idle unload，history/detail路径保持cold。
- 影响 attachment ingress、canonical append、Provider projection 与清理流程：需要 workspace attachment catalog、按内容去重、session/thread/item reference、capability 校验和引用感知 GC；CSM、history 和模型工具不得扫描 `.boxteam/attachments` 或暴露其 locator。
- 影响 ToolSelectionStore、ToolService、执行 step/Agent 生命周期和 ToolSet registry：需要区分 desired/applied ToolSet revision，在每次 model call 前的安全边界检测变化，并以 hard rebase替代“运行中 Job 永久沿用旧 Agent/工具集”的隐式行为。
- 影响 Goal、subagent/session generation、跨会话消息、团队、终端 steering、execution retry 和 checkpoint reminder 的派发方式；Goal只绑定main thread，team状态迁入Session内部child-thread ledger，跨Sessionsend只投递目标main thread；这些内部事件需要独立于真实用户 acceptance/Turn root 的 execution wakeup。
- 影响`send_message_to_session`、`read_context`、等待/监控工具及Session link解析：跨workspace/server操作不materialize目标runtime或建立共享协作状态，目标resolved main-thread identity必须进入审计和幂等记录。
- 影响Gateway federation transport、配置与权限控制：新增SSH隧道内的长期全双工WebSocket对等RPC channel、hub唯一中继、origin-preserving transit grant、channel重连/背压/多路复用，以及`permissions.federation`默认允许且运行时热发布的policy snapshot；现有持久`connection_id`不改成socket实例身份。
- 影响 Gateway 全局 Skill catalog、`${BOXTEAM_HOME}/skills/` 与 workspace/bundled Skill 名称解析，但 Gateway 仍不得读写工作区 `.boxteam/` Session 数据。
- 影响 config watcher、workspace file watcher、Job event bus及其进程装配：新增贡献定义registry、SourceReconciler、语义资源依赖DAG、ResourceTaskSupervisor、按channel隔离的通用事件服务、ResourceRegistry、activation coordinator和启动期initial reconcile/readiness gate；来源/语义identity与revision独立，监视按完整选项语义去重并按订阅引用计数。目标模块归属和各目录边界见design 5.3；不在`app/core`建立第二事件总线或把整个资源平台塞入单个Manager。
- 影响 workspace backend、文件工具与模型提示：新增 `boxteam://` VRN parser/resolver、typed capability dispatch 和安全 display URI projection，删除旧 `/.boxteam/...` Skill 虚拟挂载对模型的暴露及通用读取激活入口。
- 需要增加 CSM 追踪控制状态、resource descriptor/snapshot/activation snapshot、VRN/resource provenance、source revision/detail、稳定前缀 epoch/reason/hash/length、desired/applied ToolSet revision、Skill metadata/activation provenance 及 `skill_load` 工具 schema。
- 需要更新 Python 单元/集成测试、真实 Provider request projection、snapshot/tracked/untrack、rewind、compaction、restart 场景；跨change Web验收统一写入`tests/e2e/clients/web/test_basic_chat_tool_loop.py`，保留基础两轮item/time/order断言并增加main/child、cold runtime和跨workspace/session协作，不创建第二套E2E owner。
- 本 change 只修订规划；实现阶段继续由单一 Saver/ContextStore owner 持久化，不建立第二个 SQLite/JSONL writer。
