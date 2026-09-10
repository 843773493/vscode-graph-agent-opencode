## Context

参见 `proposal.md` 的动机。已有 `add-itemized-rollout-context` 提供 v2 canonical item、`ContextRequestPlan`、source overlay、sealed assembly 和 Saver ownership；本设计补齐统一 mutation 边界、producer、追踪、ToolSet hard rebase、wire role 与稳定前缀合同，不建立第二套上下文存储。

当前 Agent 配置、runtime identity、Todo/Filesystem/Skill/AGENTS/压缩/Memory middleware 会在不同阶段拼接 system prompt；Goal、委派、跨会话、团队、终端、retry 和 checkpoint reminder 又会直接产生 `HumanMessage` 或通过内部 `MessageRole.user` 创建 Job/Turn。最后，`PromptReplayCaptureMiddleware` 再从已组装请求反推 source 边界。这些路径会重新组装旧 system prompt、重复投影 source、绕过 checkpoint owner，或者让内部状态伪装成真实用户输入。

目标链路为：

```text
user/assistant/tool protocol ── AppendCanonicalItemIntent ───────────┐
SkillCatalog/AGENTS/team ───── ApplySourceLifecycleDecision (CSM) ──┤
tool selection/policy ──────── SwitchToolSetIntent ─────────────────┤
compaction/rewind ──────────── RebuildContextEpochIntent ───────────┤
                                                                   ▼
RolloutCheckpointSaver / ContextStore mutation owner
    │ immutable items + versioned control state + ToolSet binding
    ▼
ContextRequestPlan -> sealed ContextAssemblySnapshot
    │ Saver-issued SealedAssemblyDispatchRef
    ▼
optional stateless framework dispatch bridge -> Provider adapter
    │
    └── sealed snapshot -> history/diagnostic projector
```

## Goals / Non-Goals

**Goals:**

- 把已提交 wire context 的字节级稳定设为所有 source lifecycle 决策的首要约束。
- 为当前生产代码中的 initial instruction、tracked file、runtime event、derived compaction、真实 user、ToolSet 和模型/工具协议事实建立完整迁移闭包与唯一 owner。
- 让 canonical append、source lifecycle、ToolSet switch 和 epoch rebuild 共用同一持久化/assembly transaction 边界，但保持各自的 domain owner 与不变量。
- 把任意有效 ToolSet 变化定义为 model-call safe boundary 上的 hard rebase，并让同一 Turn 可以跨多个 prefix epoch。
- 统一 AGENTS、Skill metadata/activation、团队状态和其它内部 source 的 identity、revision、diff、追踪及恢复语义。
- 删除 PromptReplay 及所有 request-time 反向捕获，让 Provider dispatch只消费已封存 assembly；framework middleware 若不可避免，只承担无状态 bridge 职责。
- 提供只暴露名称的 `skill_load`，完整定义 `snapshot`、`tracked` 和 `untrack`。
- 保持 canonical item、CSM control state、active view、wire role 和真实用户 Turn 相互独立。
- 让 retry、restart、rewind、compaction 和多 projector 使用同一个 sealed selection 与 source provenance。

**Non-Goals:**

- 不设计按 Turn/请求次数自动到期、注入次数或即时 Skill 移除。
- 不让模型看到或传入 Skill 的绝对路径、相对路径、catalog 层级或 Gateway 内部 locator。
- Skill metadata 暂不解析 `name`、`description` 之外的 frontmatter 字段。
- 不持续监听文件系统，不使用 `rg` 周期扫盘；tracked source 只在模型请求前稳定检查。
- 不在本 change 启用 Anthropic 中途 system item；只保留明确 TODO/capability extension point。
- 不改变 Provider 原生 tool call/tool result 所要求的协议 role；本设计的 user-role 规则只约束 CSM 注入 item。
- 不把用户消息、assistant/reasoning、tool call/result 纳入 CSM revision/tracking，也不把 ToolSet 变化编码成软上下文通知。
- 不保留 PromptReplay 作为诊断、兼容或漏接 source 的 fallback，也不允许 dispatch bridge维护 Session/source/assembly 状态。

## Decisions

### 1. ContextStore 是统一 mutation owner，CSM 只管理 source lifecycle

一个 `SessionThread` 只有一个长生命周期 RolloutCheckpointSaver/ContextStore mutation owner。Session 是产品导航、共享资源与 thread catalog 容器，不是 canonical context owner；所有会影响该 thread canonical history、active view、source control state、ToolSet binding 或 sealed assembly 的变更都必须经该 owner 的同一 read snapshot 与 transaction 提交，业务层不能直接改 LangChain state、rollout JSONL、SQLite 或 Provider request。

统一边界使用语义互斥的 mutation intent，而不是把所有内容都伪装成 source：

```text
ContextMutationIntent =
    AppendCanonicalItemIntent
  | ApplySourceLifecycleDecision
  | SwitchToolSetIntent
  | RebuildContextEpochIntent
```

- `AppendCanonicalItemIntent`：真实 user input/attachment、assistant/reasoning、tool call/result 等一次性 canonical 事实；保留原始 item identity、origin Turn、协议配对和 append ordinal，不建立 source revision/tracking。
- `ApplySourceLifecycleDecision`：CSM 对 instruction/file/runtime source 的 base、delta、tracking 和恢复决策；可产生 ambient/pending source item与控制状态。
- `SwitchToolSetIntent`：应用模型可见工具及 policy 的新 desired revision，并触发 hard rebase；ToolSet 不是 message/source item。
- `RebuildContextEpochIntent`：compaction 或 rewind 对 active view 的显式重建；首次 assembly由 owner初始化 epoch。

`ContextSourceManager`（CSM）是该 owner 下的 source-specific 子管理器。producer 向 CSM 提交结构化 observation 或名称化 Skill 操作；CSM 负责 source identity、revision、diff 基准、tracking state 和 reconciliation 决策，但只能返回 `ApplySourceLifecycleDecision`，不能截获普通 canonical append、重组整个消息历史或直接生成 LangChain/Provider message。

owner 在一次 model-call preparation 中按确定顺序消费 mutation intents、完成 tool protocol convergence、source reconciliation、active-view/ToolSet selection、plan 创建与 assembly seal。任何一步失败都不得部分推进 item、source committed revision、applied ToolSet revision 或 prefix epoch。

每个 source identity 由软件内部 locator、catalog origin 和 logical facet 决定，不使用模型提供的路径。Skill 的 metadata 与 activation 即使来自同一个 `SKILL.md`，也使用不同 facet identity：

```text
skill:<catalog-entry-id>:metadata
skill:<catalog-entry-id>:activation
```

### 2. 已提交上下文前缀必须字节级稳定

将投影后的 wire context 定义为按 plan ordinal 排序的不可变 item frame 串联：

```text
W(n) = frame(item_0) || frame(item_1) || ... || frame(item_n)
```

在同一个 `prefix_epoch` 内，后续请求必须满足：

```text
W(next)[0:len(W(previous))] == W(previous)
```

稳定范围包括 item 顺序、role、content block 结构、文本、空白、编码和 Provider profile 下的 item serialization。同一 epoch 的 compatibility key 还必须固定 Provider projection profile、精确 `ToolSetRef` 及影响 root/tool visibility 的 policy hash。Provider headers、认证、request ID、模型参数和 JSON envelope 不属于 context prefix；它们继续由 request hash/profile 单独约束。

只有四个边界允许创建新的 `prefix_epoch`：

1. SessionThread/branch 的首次上下文组装；
2. 上下文压缩实际执行重建；
3. rewind 实际执行 active view 重建；
4. effective ToolSet/工具 policy 变化在 model-call safe boundary 执行 hard rebase。

普通 canonical append、source change、`skill_load`、`untrack`、团队事件、checkpoint restore、重复 model attempt 和 transport retry 都不能重建旧前缀。每个 sealed assembly 保存 `prefix_epoch`、`epoch_reason`、`parent_assembly_id`、`stable_prefix_byte_length`、`stable_prefix_hash`、ToolSet compatibility key 和新增 item references；dispatch 前验证父前缀，不一致时 fail closed。同一 assembly 的 retry 复用已封存的 Provider bytes，不能重新投影。

该约束同时禁止把新 source 合并进旧 system/user message，也禁止 Provider adapter 为满足交替 role 而合并相邻 user item。某 Provider 无法表达独立追加 item 时必须显式 reject。

### 3. ToolSet 变化必须 hard rebase，不能伪装成软上下文 append

ToolSetSnapshot/ToolSetRef 与 canonical item、CSM source state保持独立，但必须参与每次 sealed assembly 的 compatibility key。新增、删除、隐藏、恢复工具，修改工具 schema/description、执行权限或确认 policy，只要改变模型实际可见或可调用的工具事实，都产生新的 desired ToolSet revision；不区分“添加可软切换、删除才硬切换”等两套语义。

ToolSelectionStore/ToolService继续拥有 workspace/agent级 desired selection及其控制面 revision；它们不写 Session context。每个 SessionThread 的 ContextStore owner持久化最后观察到的 desired revision、当前 applied ToolSetRef/revision和产生它的 prefix epoch，并根据二者差异构造 `SwitchToolSetIntent`。因此控制面切换可以先于某个 thread 生效，但历史 assembly永远只读自身封存的 applied binding，不能用当前 ToolSelectionStore反推。

运行中的 sealed model call 永远绑定其 applied ToolSet revision，用户或控制面此时修改工具选择只能更新 pending desired revision，不能改写该请求。owner 在下一次 model-call safe boundary 按以下顺序生效：

1. 停止为旧 ToolSet 创建新的 model call；
2. 让旧 assembly 已产生的 outstanding tool call得到真实且配对的 terminal outcome：已开始执行的调用完成或返回真实失败；尚未执行且权限已撤销的调用返回绑定原 `tool_call_id` 的 policy-denied结果，不伪造“已执行”或无配对取消；
3. 原子封存新的 ToolSetSnapshot/ToolSetRef，并把 desired revision推进为 applied revision；
4. 创建 `epoch_reason=toolset_changed` 的新 `prefix_epoch`；
5. CSM只对当前 source registration/revision执行 reconciliation，assembly compiler据此重建 root context、active canonical view、source projection与 tools；
6. seal 新 assembly 后才允许下一次 Provider dispatch。

hard rebase不修改已有 canonical item identity、正文或因果顺序，也不把“工具已切换”追加成 user-role控制文本。它和 compaction都允许重新编译请求前缀，但 compaction会改变 active history view并产生/选择 compaction summary；ToolSet hard rebase默认保留同一 active canonical history，只改变 epoch、条件化 root projection与工具 binding。因此一个 Turn可跨多个 model-call-scoped prefix epoch，`prefix_epoch` 不能再被解释为 Turn 属性。

多次 selection变化若都发生在下一个 safe boundary前，可以把中间 desired状态合并为最终 revision再 seal，但控制面仍需保留可审计的 revision因果，不能把已经 applied/sealed 的 ToolSet历史改写掉。若 Provider无法在新 ToolSet下合法投影 active history中的旧 tool call/result，系统必须明确阻止 rebase，并要求适用的显式 compaction或终止该 execution；不得删除、改写或伪造历史工具事实。

### 4. 只有 epoch 顶层 root context 使用 system，后续 CSM item 使用 user

首次组装把当时已经存在的顶层基础说明、初始 AGENTS revision 和 Skill metadata catalog 编译为一个不可变 root context item，投影为 `wire_role=system`。第一条真实用户消息之后，CSM 追加的 item 不再按“完整内容还是 delta”决定 role，而按因果位置统一投影：

| CSM item | 当前 wire role |
|---|---|
| 首次组装的唯一 root context | `system` |
| `skill_load` 的完整 snapshot/tracked activation | `user` |
| AGENTS、Skill metadata/activation、team/runtime delta | `user` |
| tracked source 的 rewind 完整恢复 | `user` |
| compaction 后重新物化的完整动态 source | `user` |

这些 `wire_role=user` item 仍是 ambient runtime/source item，不是 canonical `user_input`，没有 `turn_id`，不能成为 Turn root/member。Provider 原生的 assistant/tool call/tool result role 不受此表影响。

Provider projector 当前不得把中途完整 source 提升成 system，也不得将其前插到 root context。为 Anthropic 官方部分模型未来可能支持的中途 system item 只保留关闭状态的 capability TODO；启用它必须另行修改 spec、projector profile 和缓存验证合同。

### 5. SkillCatalog 只向模型暴露名称和描述

SkillCatalog 合并三层来源：

```text
bundled resources/skills
    < ${BOXTEAM_HOME}/skills
    < ${workspace_abs_path}/.boxteam/skills
```

同名项按上述优先级确定唯一有效 entry；名称冲突、非法 metadata 或不可读文件必须显式诊断。`${BOXTEAM_HOME}/skills` 是 Gateway 级全局 catalog，默认可用于所有工作区；Gateway 负责全局 catalog/control-plane 发现，但不得写工作区 Session SQLite。Workspace 后端取得内部 catalog descriptor 后，由当前 Session 的 CSM 完成加载和持久化。

模型只看到当前有效 entry 的 `name` 和 `description`。frontmatter 其它字段暂不进入 catalog contract、prompt、hash preimage 或策略判断。工具输入只有 `name`，路径解析和稳定读取均在软件内部完成，路径不得进入模型可见参数、普通工具结果或历史正文。

Metadata 与 activation 独立生效：metadata 让模型知道可选 Skill；activation 是 `skill_load` 后实际进入上下文的 Skill 正文。首次组装已有 metadata 可进入 root system item，运行中 metadata 变化只能追加 user-role delta，不能改写 root item。

### 6. `skill_load` 使用一个工具和三个明确 mode

工具 schema 固定为：

```text
skill_load(
  name: string,
  mode: "snapshot" | "tracked" | "untrack" = "snapshot"
)
```

不存在其它即时移除工具。工具结果只返回名称、实际 mode、revision/hash 的安全标识和是否追加 source item，不返回内部路径或完整 Skill 正文。

#### snapshot

CSM 通过 catalog 解析名称，稳定读取一次当前 `SKILL.md`，追加一个完整、不可变、ambient 的 activation item。由于调用发生在用户消息之后，该 item 投影为 `wire_role=user`。随后不检查文件变化、不维护 active tracking registration，也不检查 rewind 是否移除了该 item；它只依靠 active context 自然保留，不自动到期。

存储仍保留 captured revision/hash、catalog entry identity、item identity 和受保护正文，以便 replay/audit；“不追踪”不等于丢弃已提交 provenance。

#### tracked

CSM 保存 source identity、内部 resolved path、当前 observed revision/hash、tracking enabled 状态，以及 checkpoint-versioned registration。每次 `before_model` 对 active tracked source 稳定读取一次：

- 文件未变化且最新 committed revision 仍在 active view：不追加 item；
- 文件变化：从 active view 中最新已提交可见 revision 计算到当前 revision 的一个 delta，并以 `wire_role=user` 追加；
- registration 在 rewind 目标 checkpoint 中仍为 tracked，但最新已注入 revision 不在重建后的 active view：读取并追加当前完整 revision，仍使用 `wire_role=user`；
- 同一文件在两个 model request 之间多次变化：只提交从最新已提交可见 revision 到最终稳定 revision 的一个 delta。

revision 只有在对应 item 进入 sealed assembly 后才算 committed/injected。仅观察到变化或生成临时候选不能推进 diff 基准。seal 前失败保留 pending candidate；retry 必须复用同一 candidate identity/bytes。

#### untrack

`skill_load(name, mode="untrack")` 只把当前 checkpoint 中对应 tracked registration 切换为 untracked/frozen：

- 不再执行文件 stat/hash/diff；
- 不再因为 rewind 缺失而自动恢复；
- 不追加 source 正文、撤销说明或覆盖指令；
- 不删除、重写、重排已经提交的 activation/base/delta；
- 已有内容继续按 active context、rewind 和 compaction 的普通规则自然保留或移出。

对 snapshot 或不存在的 tracked registration 调用 `untrack` 返回确定性的 `not_tracked` 结果，不伪造成功状态。之后再次调用 `mode="tracked"` 可以从当前 active view 的最新可见 revision 恢复追踪；若无可见 revision，则追加当前完整 revision。

CSM tracking state 与 checkpoint 一起版本化。rewind 到 `untrack` 之前的 checkpoint 会恢复当时的 tracked 状态；rewind 到其后的 checkpoint 保持 untracked。这避免控制状态脱离产生它的上下文因果点。

### 7. diff 基准是最新已提交且仍可见的 revision

CSM 区分：

```text
observed_revision
pending_revision
committed_revision
latest_visible_committed_revision
```

生成 delta 时只使用 `latest_visible_committed_revision` 作为 from revision。多个尚未进入 sealed assembly 的 observed/pending 变化可以在内部合并，但不能先写成 canonical item 再删除；最终只把一个 delta item 提交给 Saver。delta manifest 保存 from/to revision、base/target hash、deterministic diff hash、source identity 和被合并的内部 observation references。

同一个 tool call、model attempt、prepare 或 seal 重试通过稳定 idempotency key 复用结果。若同一 identity 对应不同正文/hash，则报告 conflict，不能按文本相似度去重。

### 8. rewind 与 compaction 建立新 epoch，但恢复 item 仍为 user

rewind 先从目标 checkpoint 恢复 checkpoint-versioned CSM registration，再重建 active view：

- snapshot item 在 cutoff 内则自然保留，在 cutoff 外则消失，CSM 不恢复；
- tracked registration 若仍有效且当前 revision 不在 active view，CSM 追加当前完整 revision；
- untracked registration 不检查、不恢复。

rewind 可以建立新的 stable-prefix epoch，但不能把 post-user source 塞回最顶层 system item。tracked 完整恢复位于重建后的上下文尾部并使用 `wire_role=user`。

compaction 可以把一个 tracked source 的 base+delta 链物化成当前完整 revision，推进 source overlay epoch，并建立新的 stable-prefix epoch。动态 source 的物化结果仍位于用户消息之后并使用 `wire_role=user`；它不能被吸收到 root system item。旧 base/delta/detail 保持不可变，直到 retention/GC 确认没有 sealed assembly 依赖。

snapshot/untracked 内容不允许重新读取源文件。压缩器只能根据 active view 和已提交受保护 detail 决定是否携带；CSM 不执行自动恢复。若压缩没有实际重建上下文，则不得借 compaction 名义改变旧前缀。

### 9. 当前生产上下文来源清单是迁移闭包

下表是本 change 编写时对生产代码的逐项审计结果。表中 ID 是迁移与测试追踪号，不是持久化 `source_identity`。ContextStore mutation owner统一所有变更的提交、排序和 seal；CSM只统一“指令、文件与运行时控制 source”的生命周期，不吞并真实用户输入、Provider 工具协议、模型输出、ToolSet或 compaction summary 的既有 domain owner。

| ID | 当前生产来源与入口 | 当前触发、落位与生命周期 | 当前问题 | 本 change 的详细目标 |
|---|---|---|---|---|
| R01 | Agent 基础说明：`ConfigService.get_agent_runtime_config()` 读取 agent `instructions.system_prompt`，`agent_factory.create_my_deep_agent()` 传给 `create_agent(system_prompt=...)` | Agent 构建时形成基础 `SystemMessage`，之后作为每次请求的 system prompt 起点 | 只有最终拼接文本，没有独立 source revision/facet；运行中配置变化无法只追加 | 注册 `agent-instructions` root source；首次组装封入唯一 root system item并记录配置 revision/hash；首个 user item 后发生变化只能追加 user-role full/delta，不能重建 root |
| R02 | 运行时身份与路径：`agent_factory._runtime_identity_system_prompt()` 注入 workspace 绝对根、首选/fallback provider/model 和路径规则 | 每次 Agent runtime 构建时追加到基础 system prompt | 与 R01 原地拼接，provider/config 重建可能改变旧前缀；字段 provenance 不可独立校验 | 注册 `runtime-identity` root source；稳定规范化 workspace/provider/model snapshot；首次进入 root，后续变更按独立 user-role source item追加；不得借 Agent 重建改写旧 assembly |
| R03 | 团队静态规则：启用 `create_team` 时 `_team_aware_system_prompt()` 追加 `TEAM_COORDINATION_SYSTEM_PROMPT` | 按最终可见工具集条件，在 Agent 构建时拼入 system prompt | 工具策略与 prompt 拼接隐式耦合，缺少“为何启用”的 source 事实 | 注册条件化 `team-coordination-policy` root source并绑定精确 ToolSet revision；运行中启停走 C02 hard rebase，在新 epoch重编译条件化 root，不向旧 epoch追加软 policy通知；动态团队事实另走 E04 |
| R04 | Todo 静态规则：`TodoListMiddleware(system_prompt=TODO_SYSTEM_PROMPT)` | middleware 在 model request 前追加 system block；`TODO_TOOL_DESCRIPTION` 同时改变 tool schema | 每请求重新拼接，靠后置捕获猜测边界；prompt 与 tool schema owner 混在 middleware 行为里 | `TODO_SYSTEM_PROMPT` 注册为绑定 ToolSet policy的条件化 root source；`TODO_TOOL_DESCRIPTION` 归 C02 ToolSetSnapshot；启停通过同一 desired revision在 safe boundary hard rebase并决定新 epoch是否 included |
| R05 | Skill metadata/index：`WorkspaceSkillsMiddleware`/upstream `SkillsMiddleware` 发现 bundled 与 workspace Skill，向 system prompt写入 locations、name、description、路径和 `read_file` 指令 | metadata 在 `before_agent` 读取一次，格式化后的 catalog 在每次 model request 前追加；当前未发现 `${BOXTEAM_HOME}/skills` | 路径泄露给模型；metadata 与 activation 混合；通过通用 read 工具激活；catalog revision、覆盖关系和运行中变化没有统一事实 | 建立三层 SkillCatalog 与 `skill:*:metadata` source，只暴露 name/description；首次 metadata snapshot进入 root，后续 catalog diff 使用 user role；activation 只能由 `skill_load` 建立。现有 `allowed_tools` 只用于工具事件 skill_names 归因，不再属于 Skill metadata/context contract；若仍需该归因必须迁到独立工具归属配置 |
| R06 | 文件系统静态规则：`FilesystemMiddleware(system_prompt=FILESYSTEM_SYSTEM_PROMPT, custom_tool_descriptions=...)` | 每次请求前追加 filesystem/environment system block，并注册/改写文件工具说明 | system block 依赖请求期拼接；其中 Skill 路径例外与旧 read 激活方案绑定；工具说明不是独立 ToolSet 事实 | 文件系统行为规则注册为绑定 ToolSet policy的 root source并删除 Skill 读取例外；所有 tool descriptions 归 C02；工具 policy变化触发 hard rebase并重编译新 epoch，不能重写旧 context item |
| R07 | 手动压缩工具静态规则：`CachePreservingSummarizationToolMiddleware(... COMPACT_CONVERSATION_SYSTEM_PROMPT)` | 启用 `compact_conversation` 时，每次 model request 前追加 system block并提供工具 | 与压缩派生请求、压缩结果的生命周期混在一起 | 静态使用规则注册为绑定 ToolSet policy的条件化 root source；工具 schema与启停归 C02并以 hard rebase生效；真正的派生摘要请求与结果归 D01，不把它们伪装成同一 CSM source |
| R08 | 工作区 `AGENTS.md`：`WorkspaceAgentsMiddleware` 初次读取完整内容，`before_model` 检测变化后追加 `workspace_agents_change` `HumanMessage`，发现 compaction marker 时又把当前完整内容拼回 system prompt | 每次 model request 前读单文件；变化时生成 unified diff；middleware 内存保存 applied/observed 内容 | 直接改 LangChain state/system prompt；控制状态不随 checkpoint 版本化；compaction 后提升回 system；重启/rewind 容易重复或错基准 | 注册 `workspace-agents:content` tracked source；首次 revision进入 root；每次 before-model 稳定读取，按 latest-visible-committed 生成 user-role delta；rewind/compaction 恢复完整内容仍为 user role；状态由 Saver/ContextStore 持久化，不使用 watcher 或 `rg` 扫盘 |
| R09 | Agent memory：`StructuredMemoryMiddleware` 可读取配置的 memory sources，并用 `MEMORY_SYSTEM_PROMPT` 包裹为 system block | 仅 `create_my_deep_agent(memory=...)` 非空时启用；当前默认生产 runtime 未传入，属于已实现但未接线能力 | 一旦启用仍会每请求拼接，正文与 source revision 无统一 owner；当前配置状态容易被误报为已生效 | 保持默认未启用；任何生产接线前必须把 memory descriptor/content 注册为独立 source：首次可进 root，后续变化按 user-role source item；memory 是不可信 reference，不能借 CSM 提升优先级 |
| E01 | Goal 生命周期：`goal_continuation`、`goal_objective_updated`、`goal_budget_limited` | `GoalRuntimeService` 构造 `PreparedInternalMessage`，再用 `create_and_run_internal()` 创建 `MessageRole.user` 消息和 Job | 内部控制状态成为普通 user message/Turn root，注入与 execution 唤醒绑死 | 作为 `goal:<goal_id>:state` ambient/pending event source提交，保留事件 revision与用户目标的数据边界；由独立 wakeup intent启动 execution，不创建真实 user Turn |
| E02 | 委派与分支结果：`delegated_task`、`generated_session_result` | `SessionSubagentService` 或 session-generation reporting 通过 internal message 准备/派发 Job；新生成 Session 的 seed prompt另有 `prepare_user_message()` 路径 | 系统委派/回报当前可伪装 user root；同为生成流程的 seed prompt没有显式区分“用户输入副本”与“内部控制” | 委派/回报注册为有幂等键的 ambient event source并以 user role投影；生成 Session seed 必须由 producer 显式声明 `user_derived_root` 或 `internal_source`，前者保留 canonical user root，后者走 CSM，禁止根据文本/role 猜测 |
| E03 | 跨会话消息：`session_message`，本地或 `/inter-agent-messages` Gateway 路由 | `simulate_user=false` 使用 `PreparedInternalMessage` 和 `create_and_run_internal()`；`simulate_user=true` 走普通用户入口 | 非模拟用户消息仍创建 user Turn；Gateway 入口只验证结构，没有 source lifecycle | `simulate_user=false` 注册 sender/target/message/idempotency provenance 的 pending event source并独立唤醒；`simulate_user=true` 明确保留 C01 真实 user 语义，不把两者合并 |
| E04 | 团队动态事实：`team_membership`、`team_task_assignment`、`team_task_update` | Team service 在持久化 board/task 后通过 internal message启动目标 Session Job | 动态团队状态和 R03 静态规则没有 facet 分离，并生成普通 user Turn | 分别注册 membership/task source identity与 revision；写 team ledger 和提交 source observation保持因果引用；以 ambient/pending user-role item唤醒，不创建 user root，也不轮询重发 |
| E05 | 后台终端完成：`terminal_execution_completed` | `TerminalSteeringService` 在资源完成后通过 internal message唤醒 owner Session | 完成事实成为普通 user message；delivery 与 terminal resource identity没有统一 source 幂等 | 注册 terminal execution event source，绑定 resource/execution/terminal outcome和 delivery idempotency；只追加一次 user-role runtime item并独立唤醒 |
| E06 | 同一执行内 retry/recovery：`missing_custom_tool_retry`、`delegated_report_retry`、`session_question_reply_retry`、`empty_response_retry` | execution step 直接构造 `HumanMessage` 作为下一次模型输入；`tool_test_retry` 只存在测试 harness | 绕过 Saver source 登记；重试次数、message id 与 sealed attempt 可能形成第二事实源 | 每种 retry 使用稳定 source kind、attempt/idempotency key注册 request-bound 或 ambient control item；仍投影 user role但不是 user Turn；重复 attempt复用 sealed bytes。`tool_test_retry` 明确排除生产迁移闭包 |
| E07 | checkpoint/runtime reminder：`checkpoint_reminder`，覆盖 interrupt、startup/turn/scope/tool timeout、execution lost/error、resource cancel 等原因 | `append_system_reminder_checkpoint()` 直接读取并改写 LangGraph checkpoint messages，追加 `HumanMessage` | 绕过 canonical item、ContextStore transaction和 source owner，是最明显的第二 writer | 删除直接 checkpoint message mutation；按 `checkpoint:<reason>:<execution/resource>` identity 经 Saver/CSM 原子提交 pending source/control outcome；恢复只读已提交事实，不从当前 runtime补造 |
| D01 | 压缩派生上下文：`compaction_summary_instruction`、`compaction_retry_marker`、派生请求 fallback `SystemMessage("Summarize...")`，以及返回主上下文的 `compaction_summary` | summarization middleware建立独立模型请求；instruction/retry 是 `HumanMessage`；结果替换 active history 中段 | 静态压缩工具规则、派生请求控制项、最终 summary 和 source materialization 容易混为同一注入来源 | 派生摘要请求使用独立 sealed derived assembly和自己的 root system item；instruction/retry 是该 derived assembly 的 request-only control；结果由 compaction owner持久化为 canonical `compaction_summary`，不是 CSM source；CSM 只在同一 compaction transaction内按规则物化各 tracked source一次 |
| C01 | 真实用户输入与附件：消息 API、明确 `simulate_user=true`、replay-as-new-turn，以及 producer 显式标记的 user-derived generated seed | `UserContentBuilder` 组装文本/多模态 blocks，runner 创建 `HumanMessage` 并持久化 user root | 当前 `MessageRole.user` 同时被内部消息复用，role 无法证明真实用户来源 | 不进入 CSM；由 acceptance/Turn owner构造 `AppendCanonicalItemIntent`，经统一 ContextStore owner创建唯一 `semantic_kind=user_input` root。入口必须携带可信 ingress intent，内部 source禁止调用此路径 |
| C02 | 模型可见工具定义：直接 built-in、Todo/filesystem/compact middleware tools、自定义扩展、MCP、team tools及 visibility/policy过滤 | Agent 构建和 request middleware确定实际 tools/tool config；Itemized middleware快照为 `ToolSetSnapshot/ToolSetRef`；当前执行 step只在 Turn/Job开始时读取一次 selection，运行中 Job保留旧 Agent引用 | descriptions 与对应静态 prompt当前散落在同一 middleware；运行中 selection变化没有 desired/applied revision或 model-call safe-boundary语义；若仅看 prompt无法复现实际工具集 | 不进入 CSM，也不编码成 context message；ToolSet registry保持唯一 domain owner，经 `SwitchToolSetIntent`进入统一 transaction。每次 model call前比较 desired/applied revision，先收敛旧调用，再封存新 snapshot/ref、建立 `toolset_changed` epoch并重编译 root/messages/tools；Skill `allowed_tools`不得改变此权威 |
| C03 | 模型与工具协议事实：assistant/reasoning、Provider tool call/result，以及 invalid args、timeout、confirmation、patch repair、compact tool生成的 synthetic `ToolMessage` | 模型流或 middleware在执行内追加 `AIMessage`/`ToolMessage`，随后进入 canonical item/checkpoint | 它们是会话事实或协议配对，不是 instruction source；误纳入 CSM 会破坏 tool_call_id 和 Provider role合同 | 不进入 CSM；由 canonical item、tool execution与terminal convergence owner构造 `AppendCanonicalItemIntent`并经统一 ContextStore owner提交，保持原生 role/配对、item identity和 plan ordinal；只能参与稳定前缀选择，不能按文本去重 |
| P01 | 请求捕获与投影管线：`PromptReplayCaptureMiddleware`、`ItemizedContextProjectionMiddleware._prompt_contributions()`、`project_context_plan()` | 逐 middleware 比较 system blocks，推断 append/replace并全部记为 request-only contribution；LangChain projector把连续贡献合成 `request-only-system-prompt` | 这是从最终请求反推 provenance，不是 producer 权威；replace/合并会丢 item边界并破坏稳定前缀；无捕获时 synthetic fallback掩盖漏接来源 | 删除 PromptReplay、捕获标签和 instrument 链；废弃 `ItemizedContextProjectionMiddleware` 的 prompt/tool/context反向捕获。每个 R/E producer在 seal 前显式登记；若框架必须保留 middleware hook，只实现无状态 sealed-assembly dispatch bridge，按 Saver-issued reference转发精确 messages/tools/frames，缺失或不匹配即 fail closed |

迁移闭包之外，`app/tool_testing` 的 `tool_test_retry` 只是模型测试 harness；当前未接线的 knowledge/safety 配置也不是生产上下文来源，不得为了“完整”预先实现。R09 memory 只有在显式传入 sources 时才算活跃，测试存在不等于默认生产已启用。

### 10. 删除反向捕获，middleware 最多只是无状态 dispatch bridge

`PromptReplayCaptureMiddleware`、`_PROMPT_REPLAY_LABELS`、`_instrument_prompt_replay()` 及其 request block diff/replace 推断链全部删除，不保留“仅用于诊断”的运行模式。诊断只读取 sealed assembly manifest、source refs、ToolSetRef、frame hash和最终 dispatch outcome；它不能再次观察 framework request并生成另一份 provenance。

现有 `ItemizedContextProjectionMiddleware` 的以下职责全部废弃：

- 从 `request.system_message` 或 `request.messages` 反向生成 `ContextContribution`；
- 从 request-time tools反向创建权威 `ToolSetSnapshot`；
- 在 middleware 内调用 prepare/reconcile/seal，或维护 source/session/request状态；
- 在缺少 capture 时生成 assembled-system-prompt fallback；
- 合并相邻 system/user blocks、重新排序或重新格式化 sealed item。

Context compiler、CSM、ToolSet registry和 Saver owner必须在进入 framework model-call hook前完成 canonical append、tool protocol convergence、source reconciliation、ToolSet hard rebase、plan创建和 seal，并向执行层交付不可变 `SealedAssemblyDispatchRef`。该 reference至少绑定 Session、execution/model-call attempt、assembly、prefix epoch/reason、Provider profile、messages/frame hash和精确 ToolSetRef/policy hash，不能只传可被复用到其它请求的 assembly id。

若 LangChain 等框架必须通过 middleware 才能替换最终 model request，则只保留一个 `SealedAssemblyDispatchBridge` 式无状态适配器：

1. 从本次 execution context取得 `SealedAssemblyDispatchRef`；
2. 通过只读 Saver dispatch port解析已经 sealed 的 messages/tools/Provider frames；
3. 校验 Session、model-call attempt、Provider profile、prefix/frame hash和 ToolSet binding；
4. 把精确 sealed payload交给下一层，既不读取旧 request内容推断来源，也不创建或修改任何持久状态；
5. 在 Provider 调用前再次验证最终 bytes/hash，发现 bridge 之后仍有 middleware改写请求时 fail closed。

bridge必须是最后一个可影响 model request的 framework hook；其后只允许不修改 payload的 telemetry。相同 sealed assembly retry重复解析同一 reference并得到相同 bytes，不使用进程缓存、middleware state或“上一次请求”作为基准。若框架允许 execution/provider adapter直接消费 sealed payload，则删除 `ItemizedContextProjectionMiddleware`，不为了保留类名而创建空兼容层。

考虑过保留 PromptReplay做运行时对账，但它仍会形成第二 provenance来源，且无法证明 middleware diff与 canonical item边界一致，因此拒绝。对账改为“producer registration + sealed manifest + dispatch byte hash”的正向验证。

### 11. 所有 producer 使用统一 mutation 边界和各自 domain owner

- **初始 root producer**：R01–R07 以及显式启用的 R09 在首次组装时提供独立 provenance，由 compiler 只在该合法边界编译成唯一 root system item。
- **文件 producer**：R08 与 Skill activation 使用 CSM tracked/snapshot 规则；activation 只能由 `skill_load` 建立，通用 read 工具不承担 Skill 激活语义。
- **事件 producer**：E01–E07 先提交 ambient/pending source item，再通过独立 execution wakeup intent消费；不得调用普通 user acceptance 创建 Turn root。
- **canonical append producer**：C01/C03 保持 acceptance/Turn、model stream和 tool execution的 domain owner，通过 `AppendCanonicalItemIntent`进入同一 ContextStore transaction，不建立 CSM tracking。
- **ToolSet producer**：C02 由 ToolSet registry产生 `SwitchToolSetIntent`；所有有效变化在下一个 model-call safe boundary hard rebase，不追加软上下文通知。
- **派生 owner**：D01 保持 compaction owner并通过 `RebuildContextEpochIntent`显式重建 active view；“统一 mutation owner”不等于把所有事实改造成 CSM source。
- **投影管线**：P01 的反向捕获全部删除；可选 bridge只验证和转发已登记、已 sealed 的事实，不根据 LangChain 最终消息形状反推 lifecycle。

display/history projector 可以显示安全摘要和 provenance，但不得把这些 item 投影成普通用户对话，也不得把展示内容反写成 source 正文。

### 12. 失败默认阻断，不读取当前文件修补历史

required source 的 detail、hash、revision、owner、stable prefix 或 catalog identity 校验失败时，assembly seal/dispatch 必须失败。optional source 只能通过 included=false、availability、omission/loss 明确记录。

恢复旧 assembly 时只能读取已提交 item/detail/manifest，不得重新读取当前 AGENTS、Skill 或团队状态拼出历史请求。Provider adapter 若合并相邻 message、移动 system item、改变已提交 frame序列化，或在新 ToolSet下无法表达 active history中的旧 tool call/result，也必须明确拒绝；后者只能通过显式 compaction或终止 execution处理，不能静默改写协议事实。

### 13. SessionThread 是 CSM、stable prefix 与 ToolSet applied state 的唯一 owner key

CSM 的所有 source registration、tracked revision、latest-visible-committed 基准、untrack 状态与 source detail 都必须绑定 `(session_id, thread_id, source_identity)`。同一个 workspace file/Skill/team source 可以被多个 thread 各自追踪；其文件 revision 可以相同，但每条 thread 的 active view、已提交 base/delta、rewind 结果和下一次注入决定必须独立，不能因 source hash 相同而跨 thread 复用 CSM control state 或 context item。

`RolloutCheckpointRuntime` 仍是 workspace singleton 的组件装配容器，不成为任何 Session 或 thread 的第二 writer。它通过显式 `ThreadRuntimeBinding(session_id, thread_id, graph_binding, execution_id, model_call_id)` 取得唯一 Saver/ContextStore port。`SealedAssemblyDispatchRef`、CSM observation、ToolSet switch 和 epoch rebuild 都必须携带同一 binding；缺失、owner 不一致、或尝试以 session root/`checkpoint_ns` 替代 thread 时 fail closed。

产品层未指定 thread 的普通 Session 聊天/历史 API 只从权威 catalog 解析 `main_thread_id`，并把实际 ID 返回给调用方；内部 producer、subagent、retry、rewind、compaction 和 dispatch 必须显式给定 thread。delegated child thread 的结果如需进入 main thread，使用带 source-thread provenance 的 ambient/runtime item 由 main-thread owner 追加，不能共享 source state、复制 child item 为 user root，或将两条 thread 的 stable prefix 合并。

GraphBinding 的持久化与 CSM owner 对齐：graph factory/blueprint 可以跨 thread 缓存，已编译 graph、工具和 middleware 不得捕获 SessionThread；每次 invocation 注入 binding。无法解析精确 graph revision 的恢复必须停止在 dispatch 前，不能通过创建新 context epoch 或改写旧 prefix 修复。

thread 物理 locator 也属于 owner binding。main thread 的 locator 固定为 `threads/{main_thread_id}`；其它 durable thread 固定为 `threads/YYYY/MM/DD/{sha256(thread_id)[0:2]}/{thread_id}`，日期来自不可变 UTC `created_at`。CSM、Saver 和 dispatch bridge只能消费 thread catalog/resolver 已验证的 locator，不能按规则自行拼接，不能扫描目录，也不能把 `checkpoint_ns` 加入物理路径。main thread 不使用 `threads/main` 别名，所有 thread 的叶目录名都等于真实 `thread_id`。

附件正文是 workspace 级外部 blob，不属于 CSM source detail或任一 thread rollout正文。固定物理根为 `.boxteam/attachments/YYYY/MM/DD/{digest[0:2]}/{blob-id}`，由 `.boxteam/attachments/catalog.sqlite` 解析逻辑 attachment/variant、digest、locator、session/thread/item owner refs、protection、retention和 tombstone。CSM/ContextStore canonical append只保存稳定 attachment reference与 provenance；assembly seal、Provider projection和工具读取必须通过 capability检查 owner/view membership、hash和length。相同 blob可跨Session物理去重，但权限、引用释放和历史可见性不能共享；GC不得因一个Session删除而回收仍被其它 owner引用的blob。

## Risks / Trade-offs

- **[post-user 完整 source 使用 user role，指令优先级低于 root system]** → 这是当前统一语义；通过明确 source framing 表达内部上下文，不偷偷提升 role。Anthropic 中途 system 能力只留 TODO。
- **[untrack 不会立即消除 Skill 影响]** → 工具名称和结果明确描述“停止追踪”；既有上下文继续存在，不承诺即时移除。
- **[Provider adapter 自动合并连续 user message]** → 对 projector 输出做 frame-level golden test 和 dispatch 前 stable-prefix 校验；无法关闭合并的 Provider profile 显式 reject。
- **[bridge 之后仍有 middleware 修改 request]** → bridge必须位于最后一个 request-mutating hook，Provider preflight比较 sealed frame bytes/hash；任何差异阻断 dispatch，telemetry只能旁路观察。
- **[Gateway 全局与 workspace Skill 同名]** → 使用固定优先级和内部 catalog entry identity；模型只看到唯一有效名称，诊断保留 origin 但不泄露路径。
- **[文件读取期间变化导致错误 delta]** → 使用 stat/signature/hash 的稳定读取循环；无法获得一致快照时阻止本次 dispatch。
- **[旧会话没有 source manifest]** → 返回明确 migration/source-mismatch，不从当前文件静默回填。
- **[多个 producer 并发提交同一 source]** → 在单一 owner transaction 中使用 source/revision/diff 幂等键和 conflict 检查。
- **[用户快速连续切换 ToolSet]** → safe boundary前只把最终 desired revision用于下一次 seal，但保留每次控制面revision及因果审计；已经 applied/sealed 的 ToolSet不可覆盖。
- **[切换时存在 outstanding tool call]** → 先按旧 assembly和原 `tool_call_id`产生真实 completion/failure/policy-denied terminal outcome，再 hard rebase；绝不制造未配对取消或虚假成功。
- **[Provider无法在新 ToolSet下投影旧工具历史]** → 阻止 rebase并返回具体兼容性错误；只有显式 compaction可建立另一 active view，不能由 adapter静默删除历史。

## Migration Plan

1. 先建立以 `(session_id, thread_id)` 为 key 的统一 `ContextMutationIntent` 端口、单一 thread owner transaction与版本化 migration，使 canonical append、source lifecycle、ToolSet switch和 epoch rebuild都不再旁路 ContextStore，同时不修改旧 JSONL item bytes。
2. 增加 stable-prefix epoch/reason/manifest、ToolSet compatibility key、desired/applied revision和 CSM source/tracking contract。
3. 建立 bundled、Gateway-global、workspace SkillCatalog 和 metadata/activation facet，先验证名称解析与路径不泄露。
4. 实现 `skill_load` 的 snapshot/tracked/untrack 及 checkpoint-versioned control state，删除通用 read 激活和任何即时移除规划/入口。
5. 按 R01–R09、E01–E07 的迁移闭包依次迁移初始 instruction、文件和事件 producer，删除直接内部 `HumanMessage`、checkpoint message mutation 和 user Turn 注入路径。
6. 让 ToolSelectionStore/ToolService、execution step、ToolSet registry与 assembly compiler在每次 model call safe boundary执行 C02 hard rebase，覆盖 outstanding call convergence、同 Turn多 epoch和 Provider历史兼容性失败。
7. 分离 D01/C01/C03 owner，删除 PromptReplay与 P01 的所有来源反推、ToolSet反向快照和 merged system fallback；框架确需 hook时把 `ItemizedContextProjectionMiddleware` 替换为无状态 sealed-assembly dispatch bridge，否则直接删除，并接入 rewind、compaction、restart 和 retry。
8. 完成 Provider frame、真实请求日志、checkpoint manifest、history projection、Web/tool-loop 和架构审计后再启用新路径。

回滚只能撤销尚未提交的 CSM/assembly transaction。已经提交的 source item、旧 assembly 和历史不得删除或原地修改；新 projector 无法恢复 required source 或稳定前缀时，保留旧事实并阻止新 dispatch。
