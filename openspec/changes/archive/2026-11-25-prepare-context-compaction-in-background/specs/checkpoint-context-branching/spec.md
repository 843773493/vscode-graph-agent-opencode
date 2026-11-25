## RENAMED Requirements

- FROM: `### Requirement: 用户操作使用 Turn anchor，compaction 保留 Message anchor`
- TO: `### Requirement: 用户操作使用 Turn anchor，compaction 使用语义 Item anchor`

## MODIFIED Requirements

### Requirement: Compaction 创建 SQLite context view

系统 SHALL将compaction表达为SQLite中新的context view和控制事件，不创建新的JSONL segment或控制记录。摘要和保留的canonical item继续作为不可变item追加到rollout.jsonl，新的view通过item range/reference选择摘要和保留范围。实际compaction是source/root重建的prefix epoch边界；compaction事务提交view及`PendingPrefixEpochTransition(reason=compaction)`，但pending记录没有wire bytes且不是applied epoch。下一次真正model-call preparation可以精确复用source base/delta，也可以从activation coordinator冻结的ResourceRegistry内存snapshot取得tracked published revision并物化为完整source，然后把source decision、首个新epoch assembly seal与transition消费原子提交。仅为移除已处理 head-internal notice refs 的`runtime_notice_consumed` transition必须复用同一 transition/applied owner，不能当作compaction/rewind source boundary；与compaction同 seal 时由该 owner保留两个reason事实并原子提交。source restore/fork/copy consumers MUST 过滤 `runtime_notice_consumed`，不从其恢复source checkpoint或继承fork lineage。snapshot不可用时保留compaction view/transition但不产生半assembly或dispatch，且不读取文件、网络或其它provider。所有第一条真实用户消息后的summary/source完整或增量内容必须保持独立user-role item。source/detail失效不能被解释为物化理由来修复旧sealed assembly；精确恢复仍显式失败。

后台生成 SHALL 只登记冻结替换提案，ready不构成compaction。应用时原owner必须核对当前selection、替换覆盖和全部尾部，并原子记录候选唯一消费、summary Item、新view和pending transition；尾部纯追加不一律使候选失效。未采用候选不属于fork历史；fork默认只物化选定的真实view，在目标第一次执行前走统一容量准备，不依赖父Thread候选，也不改变父view。

#### Scenario: 压缩后继续运行

- **WHEN** compaction 成功提交并开始下一轮 Agent 执行
- **THEN** 新checkpoint引用包含摘要和必要尾部item的SQLite context view并登记pending compaction transition，后续item继续追加到同一个rollout.jsonl；下一次model-call preparation若精确复用source overlay或从冻结Registry activation snapshot物化tracked published完整user-role revision，都必须与首个新epochassembly及transition消费原子提交，不得修改旧item、请求期读源或用当前文件修复旧assembly

#### Scenario: 压缩后读取原始历史

- **WHEN** 调用方请求 compaction 之前仍未被 pruning 的历史
- **THEN** reader 通过 SQLite 旧 view 读取原始 item，且不把 compaction 控制事件作为模型请求 item 或用户消息

#### Scenario: compaction 前的 source overlay 保留审计 lineage

- **WHEN** `AGENTS.md` 或已应用的 skill source 在 compaction 前有 base A 和 A→B 增量
- **THEN** compaction的新epoch可以继续精确引用A与A→B，也可以按明确materialization policy从冻结Registry activation snapshot取得并引用B的完整revision；仅owner声明`root_eligible`且实际新epoch重建时可合并唯一system root，`tail_only`仍为独立user-role item。A与A→B的item/provenance保持可审计，不被物理覆盖或删除；source/detail失效作为旧assembly明确错误，不能触发静默重建或当前源读取

#### Scenario: fork时父Thread有未采用候选

- **WHEN** 用户从父Thread选定范围创建fork，而父Thread正在生成或已保存未采用摘要
- **THEN** fork只复制选定view及其原有必要正文依赖，不带入未采用候选或扩大来源范围；目标按自身模型准备，父Thread候选和active view不被目标压缩修改

#### Scenario: 压缩后fork不展开摘要provenance

- **WHEN** 用户选择的fork view包含已应用summary和保留尾部
- **THEN** 保留该view的真实summary及尾部，按原fork正文依赖规则物化；摘要provenance用于审计，不因此把已排除原历史重新加入目标上下文

#### Scenario: 仅物化摘要的fork再次压缩

- **WHEN** 目标Thread保留来源摘要但未物化其全部原文，随后压缩该摘要及目标新增的闭合历史
- **THEN** 沿原候选与派生请求链完成生成和采用，不继承来源候选任务；本次coverage反映实际摘要输入，新摘要provenance保持展开后的历史归属，预算模拟与采用一致
- **AND** 原文回读沿已复制的行范围保留原位置，未物化原文明确返回detail-unavailable，不回读父Thread、不把摘要或派生输出冒充原文；目标后继候选仍可继续压缩
- **AND** Agent Context仍能返回该真实selection；无法从未物化原消息组取得准确原消息数量时，沿既有optional计数字段表示不可得，并将原因纳入原partial_errors及分页预算，不伪造0或用当前记录数冒充原消息数


### Requirement: 用户操作使用 Turn anchor，compaction 使用语义 Item anchor

系统 SHALL 继续允许用户可见的 rewind、replay 以及从历史 Turn 发起的上下文操作使用稳定的 `turn_id` 和 `inclusive`/`before` 语义；backend resolver MUST 通过 `RolloutCheckpointSaver` 读取已提交的 active-lineage source view，并解析到 `TurnRecord.root_input_item_id` 对应的 canonical item 起点。业务层和 projector 不得直接扫描 RolloutStorage、AppendWriter 或内部 context reader。内部 compaction、fork 和恢复流程可以直接使用细粒度 durable item anchor；interrupt 首先使用当前 stream 的内存 cursor/ItemDraft，只有需要跨重启恢复或审计时才保存对应的 item reference。request-only context reference 与 pending runtime notice 不属于 view，不能直接作为 durable operation anchor。Turn 入口由后端解析精确 view；消息 fork 入口固定点击 item，支持显式绑定其展示 view，后端按「Fork item 定位与前缀捕获保持一致」冻结真实来源，不要求调用方预知整体 revision，不传真实 offset 或物理 message 序号。全分支 item-only fork 定位与此处 Turn 的 active-lineage resolver 是不同的选择语义，不能借本项放宽 rewind/replay/compaction。

任何将被继续执行或编译为 Provider request 的目标 view MUST 在 tool protocol 上闭合。若解析后的 item anchor 位于 assistant tool-call group 与任一匹配 terminal tool result 之间，resolver MUST 返回 `tool-protocol-boundary-conflict`，保持原 view 且不得创建新 view、branch、checkpoint 或 prefix epoch；响应可以提供不含内容正文的前后最近安全 anchor，但不得自动移动用户选择、合成 result 或按物理序号猜测边界。read-only history 可以保留 partial/incomplete group 用于审计，但不得把它标记为可执行。

系统 SHALL 保留 item 级定位能力。自动 compaction、用户主动 compaction、重放和 durable interrupt finalization 均可以使用 canonical item 作为边界；不得因为 Turn 或 LangChain message projection 存在而把操作强制对齐到 message 末尾。实时 interrupt 可以只停留在内存 cursor，不能因此要求每个 raw chunk 都成为 SQLite anchor。所有 durable anchor MUST 明确记录 `inclusive`/`before`、source view/branch 和可恢复性，并使用当前格式的稳定语义Item身份；流式draft局部位置只作实时处理，不建立通用Part身份。任意字符/token offset、message数组索引或物理行号不得单独作为durable anchor。

#### Scenario: Turn 操作解析到 tool call 之前

- **WHEN** 用户从包含 reasoning、assistant output 和 tool call 的 Turn 发起 before replay
- **THEN** resolver 将 Turn 入口解析为 tool call item 之前的精确 anchor，保留前置 item，不构造或修改 LangChain message 来表达边界

#### Scenario: 语义 Item 流式中断

- **WHEN** stream 在 assistant_output item 的某个draft局部位置被用户中断
- **THEN** finalization 保存 partial 状态；只有跨重启恢复或审计需要时才保存 item 停止reference，恢复不会把该事实扩展为完整 assistant message

#### Scenario: item anchor 不属于 source view

- **WHEN** 调用方提交的 item anchor 不在目标 branch/view 的有效 lineage 中
- **THEN** 系统返回明确的 stale/unreachable anchor 错误，不按最大 message sequence 或最新 checkpoint 静默替换边界

#### Scenario: item anchor 切入 tool 调用与结果之间

- **WHEN** rewind、replay、compaction 或 fork 将一个 assistant tool-call group 与其任一匹配 terminal tool result 分隔到 active view 边界两侧
- **THEN** 系统返回 `tool-protocol-boundary-conflict`，不改变原 view、不创建目标，并返回可选择的安全 anchor metadata；只有只读历史可以展示该 partial group，执行投影不得消费它

#### Scenario: system reminder 位于新 Turn 之前

- **WHEN** source view 中最后一个执行因中断产生 pending runtime notice，随后用户提交新的普通输入
- **THEN** resolver 将新输入的 `root_input_item_id` 作为新 Turn 起点；pending notice 只按其 `notice_for`/assembly 关系参与请求，不因其 wire role 或物理位置取代新 Turn root

#### Scenario: 从被多次压缩的历史 Turn 发起操作

- **WHEN** 用户只提交 `turn_id=turn-1`，当前 active head 经历多次 rewind 和 compaction，最新 view 不再具有所需的原始 Turn anchor
- **THEN** 后端沿该精确 thread 的 active-head ancestry 查找最近可恢复所请求 Turn anchor、正文与 checkpoint/channel 状态的 view，并核验精确边界及 tool protocol closure；不使用其它分支的同 ID 替代
- **AND** 前端和调用方不需要知道 `view_id`，该 Turn 入口不改变 item-only fork 的全分支选择语义

#### Scenario: Compaction 切入 Turn 中间

- **WHEN** compaction 的 canonical item anchor 位于 Turn 中间且满足 tool protocol closure
- **THEN** compaction 保存精确 cutoff、摘要与保留成员，不强制对齐 Turn 或 LangChain message 末尾
- **AND** `context_view_turns` 只是完整可展示 Turn 的派生定位，不改变 canonical membership；Agent 的 `full` reader 依精确 view selection 恢复摘要与保留 item

#### Scenario: Turn 只有部分内容留在当前 view

- **WHEN** 用户通过 Turn 入口发起操作，但当前候选 view 只保留部分内容或 compaction summary，无法恢复所请求的精确 anchor 与必要正文
- **THEN** resolver 继续沿 active lineage 查找符合该操作边界及协议闭合要求的可恢复 view
- **AND** 没有合法来源时明确报告不可达或不可恢复，不把 summary-only view 冒充原始 Turn、不扩大用户选择；item-only fork 仍遵守其独立合同

#### Scenario: 同一投影消息内部存在安全语义切点

- **WHEN** 一条原assistant消息中的多个语义Item存在满足消息组重建、保护载荷和工具闭合的切点
- **THEN** compaction可以在该Item边界规划，不强制保留整条LangChain消息或整个Turn；原声明顺序及工具执行关系不因摘要被重排

## ADDED Requirements

### Requirement: 压缩后所有执行和查询投影使用同一权威view

active view selection SHALL 是当前有效上下文的唯一权威；Graph messages、Agent Context、replay、fork、历史及模型请求都必须通过原上下文owner解析其明确view。应用compaction时必须保存与checkpoint恢复一致的view绑定及必要channel版本，旧内存messages不得重新追加已排除成员。不得再维护按 `_summarization_event` 消息offset切割的独立有效上下文或空模型回复驱动的压缩续跑链；过去checkpoint事实保持不可变，但不作为当前读写的兼容fallback。

#### Scenario: 应用候选后Graph保存旧内存通道
- **WHEN** view已切换为摘要与保留尾部，下一Graph checkpoint仍携带压缩前messages
- **THEN** 原owner依据已提交view及其恢复绑定核对／拒绝过期输入，不复活旧成员、不重复追加summary；后续真实请求及Agent Context显示同一selection

#### Scenario: replay穿过多个压缩边界
- **WHEN** 原Thread多次压缩后从合法历史anchor执行replay
- **THEN** 使用该anchor解析得到的权威来源view及原协议闭合事实，不按最新event的旧数组索引截另一份历史；新分支和当前查询均沿原owner恢复
