## MODIFIED Requirements

### Requirement: Fork 支持三种独立模式

系统 SHALL 将上下文范围、目标创建意图和资源继承选项作为三个独立维度，所有入口共用一次 fork 操作：

1. `view_prefix_fork`：在指定且冻结修订的 view 中，选择截至 item/content-part anchor 的有效前缀，支持 `inclusive|before`；不是从物理 JSONL 开始或最近边界机械截取。
2. `context_fork`：自动选择精确 source thread 的 active view 与最近已完成的安全截止点，随后消费同一个 `view_prefix_fork` 内核；不得保留第二套复制实现。
3. `full_rollout_copy`：对选定 source thread 的已提交 rollout、SQLite 历史索引/checkpoint/view/branch 及声明的历史正文文件建立独立磁盘快照；不是整个 Session 或 Workspace 的无差别复制。

公开输入、DTO、工具、前端、测试和文档 MUST 将旧范围名统一替换为 `view_prefix_fork`，不保留旧名 alias、双模式或旧 remap 链。target 必须是新建且为空的 thread namespace；默认在来源所属 Session 内创建 subthread，也允许指定新 Session 的 main thread。来源与目标可以位于不同工作区或 Gateway，寻址与授权遵守本 spec 后续要求，不直接打开远端 SQLite 或使用 send/read/wait grant。

复制的历史 item/message/Turn/tool-call/execution/model-call/assembly/detail 等 local ID、历史时间、内容 hash 和已封存请求 hash MUST 保留。全局引用 MUST 由当前资源所属 Gateway/Workspace、`(session_id,thread_id)` 及 local ID 联合解析，不以裸 local ID 跨 owner 查找。target 创建自己的 Session/thread ID、资源根、创建时间及 copy-origin 归属 metadata；历史正文中的原 owner 和 URI 是原执行事实，不为迁移重新生成或字符串替换。当前存储归属与历史执行 scope MUST 分离，并通过冻结的来源 namespace 绑定在目标本地解析、核验；该绑定不是授权读取源会话的重定向。MUST NOT 为每个历史实体重新分配身份、累积逐项 identity mapping 或重算历史业务 hash。

完整快照保留 JSONL、历史工具输出正文、声明的 detail/附件正文文件和历史请求快照原字节；工作区共享 blob 不纳入 thread 目录重复复制，附件及其它引用资源消费 requirement「Fork 引用资源必须按统一目标处理合同复用或迁移」。view 前缀按索引选择和必要正文依赖复制原记录；目标文件布局改变时只由目标索引记录真实 offset，不修改正文、local ID 或 hash。新 branch/view 和后续新输入的身份由 target owner 正常创建。

plan registry MUST 只保存真实创建的 plan 注册，保留原 initial/draft/sealed manifest、revision、creation hash、seal input hash 和 seal hash，不生成空 draft/creation hash 的目标伪注册，不为复制重算历史摘要。目标本地冻结 namespace binding MUST 只授权历史 reader 校验原执行 scope；create/revise/seal 等写入仍校验当前数据库 owner，后继新请求由目标 owner 正常创建。旧历史 remap 专属的 registration-origin、provenance/source-manifest 分支及其写入器 MUST 随旧链物理下线，不迁移旧开发数据；这不删除资源 owner 按统一资源合同执行的真实附件导入。

源 Job、领取锁、连接、执行准入、未完成工具执行和自动续跑状态 MUST NOT 恢复为 target 活跃状态。复制范围通过轻量快照归属 metadata 标记为不可恢复的历史执行；不得把原历史状态批量改写为 cancelled、改写 final item 或伪造目标执行事实。对 imported history 的 `resume_turn|dispatch_replay` MUST 返回 `turn_not_resumable`；明确的 `replay_as_new_turn` 或新用户输入才创建新 target Turn/root/acceptance/execution，并记录来源。Goal、调试方案、终端和浏览器等额外资源默认不继承，且不因模式名为 full 而自动恢复。

默认 `context_fork` MUST 只沿显式 source thread 的 active lineage 选最近已完成 normal Turn；无 normal Turn 时可使用该 thread 已完成且与 checkpoint/view 精确绑定的 internal execution/admission。空 rollout 可创建空 target。缺少真实来源、required 正文或可恢复状态时明确失败，不回退 main、sibling 或全局最新 checkpoint。选中 item 必须已提交且目标前缀满足 tool protocol closure；source 后续有执行或追加时，不得混入冻结前缀，也不得取消 source 来伪造成功。

已注册且仍 active 的全新 Session/thread 尚未物化 rollout 时，显式选择 fork 来源且未携带历史 view/item selector 的入口 SHALL 在同一来源生命周期保护及唯一 rollout 写锁下初始化真实空 rollout，再冻结其 view/revision。普通历史浏览 MUST 保持只读；携带历史 selector 时不得用新空 view 替代原来源，既有目录损坏或历史文件被删除时也不得自动重建。空来源没有 item anchor，UI 的默认 `view_prefix_fork` 不可提交；用户可显式选择 `full_rollout_copy` 创建 idle 空分支，`context_fork` 继续使用既定空上下文合同，不伪造 Turn、execution 或消息。

#### Scenario: view 前缀保留摘要及边界前仍有效成员

- **WHEN** 冻结的 view 为 `[摘要 S, 保留 C, 新增 D, 新增 E]`，用户 inclusive 锚定 D
- **THEN** target 的可见上下文为该 view 中的 S、C、D，顺序保持，E 不进入；C 不因物理上位于压缩边界前而丢失，必要正文依赖另行解析

#### Scenario: 默认有效上下文与显式锚点共用内核

- **WHEN** `context_fork` 在显式 source thread 解析出最近完成的安全锚点，或 UI 直接提供同一 view/revision/item
- **THEN** 两入口取得相同有效前缀及依赖，使用同一 capture、资源检查、staging 和发布合同，不另建 context 专属复制器

#### Scenario: 初始 internal child 可成为精确来源

- **WHEN** source child 没有 normal Turn，但其 seed/reference execution 已完成且 checkpoint、selection、detail 与 admission 绑定一致
- **THEN** fork 继承该精确 child 的有效正文，保留来源事实，不回退 main、不伪造 Turn、notice 或 target Job

#### Scenario: 全新空会话显式选择完整复制

- **WHEN** 用户打开尚无 rollout 的全新会话，从会话入口选择 fork 来源并显式切换为完整复制
- **THEN** 普通打开及历史读取不创建 rollout，来源选择冻结真实初始化的空 view/revision；目标发布为空闲分支，首次聊天仅创建目标的新 Turn，main 历史保持为空

#### Scenario: 缺失历史不被空来源初始化掩盖

- **WHEN** fork 来源请求携带无法解析的历史 view/revision，或已物化来源的目录、索引或锁文件缺失
- **THEN** 明确拒绝，不初始化替代历史、不创建目标，也不返回伪造的空来源

#### Scenario: 未提交或协议不完整的锚点拒绝创建

- **WHEN** 点击 item 尚未提交、修订不可读取，或截止点切开 tool-call/result group
- **THEN** fork 明确失败且无可见 target；可以返回安全锚点 metadata，但不自动移动点击位置、不加入后续 item 或合成结果

#### Scenario: 完整快照不重写历史执行

- **WHEN** source 包含正在运行或中断的 execution，用户选择 `full_rollout_copy`
- **THEN** target 保留冻结时的历史正文、ID、状态和 hash，当前目标执行状态为空；旧执行只读不可 resume，新输入创建目标的新执行，source 继续原运行

#### Scenario: 新输入与重跑产生新目标事实

- **WHEN** fork 发布后用户发送消息或明确执行 `replay_as_new_turn`
- **THEN** target 新建 Turn/root/acceptance/execution 和自己的当前请求，不复用历史准入 key，不恢复旧 Job，不重算旧 plan/request hash

### Requirement: Fork 来源不形成默认运行时依赖

target SHALL 保存唯一 copy-origin，绑定 source/target 资源身份、精确 thread/view/revision/checkpoint、范围与 anchor、捕获 snapshot、历史 namespace 归属和资源处理回执。历史运行数据 MUST 在目标本地可读，不依赖 source runtime、JSONL/SQLite/detail 真实路径或源 URI 在线解析。历史 owner 校验仍须对照捕获的不可变来源 binding、完整内容校验和 target catalog 归属；MUST NOT 为保留旧字节而跳过 owner/hash 检查或全局重定向任意 source URI。

`detached` 为默认；source view 压缩、清理或 source thread 删除不影响已物化正文。独立 Session target 在 source Session 删除后仍可读历史及继续聊天。同 Session subthread 仍遵循所属 Session 的删除生命周期，删除整个 Session 时随之删除，不能宣称独立于所属 Session。

`pinned` 仅增加来源审计 retention，不成为 target 请求的读取路径。它 MUST 在 capture 前由既有 fork journal 建立唯一 durable claim；target commit 后只激活同一 claim，abort/target 删除先持久化 release intent，释放 target gate 后再单独释放 source claim。claim 与 lease 原子登记，无墙钟自动到期，不同时持两端 gate/事务。同 Session retention 只保护来源 view/detail；整个所属 Session 删除时先释放内部 claim，不允许 child 对所属 Session 形成自阻塞。跨端 pinned 仅在 source owner 支持对应 retention 能力且获得该操作授权时可用，否则明确拒绝。

#### Scenario: 删除源后独立副本仍可用

- **WHEN** detached target 位于独立 Session，已发布后删除 source Session
- **THEN** target 能读取自己的历史、已保留的引用正文、按资源处理回执复用或导入的附件，并接受新输入；没有 source 运行数据读取

#### Scenario: 同 Session subthread 随容器删除

- **WHEN** target 是同 Session subthread，source view 已被清理，随后删除整个 Session
- **THEN** source view 清理不破坏副本正文；整个 Session 删除正常清理 target 及内部 retention，不形成自阻塞

#### Scenario: pinned claim 与删除竞争

- **WHEN** 独立 target 的来源审计 claim 与 source 删除竞争
- **THEN** 删除先关闭准入则 claim 不建立；claim 先提交则删除报告具体 blocker；target 恢复仅激活既有 claim，删除/abort 定点释放且不同时持两端 gate

### Requirement: Fork 物化必须可恢复提交

fork SHALL 使用既有 operation journal 固定请求 preimage、来源 snapshot、目标创建意图/归属、资源处理清单和 staging 发布状态。重复同一 fork key 返回原目标；不同来源、锚点、目标或继承选项不得覆盖同一操作。中间文件、索引、资源保留关系和 Goal 均不得提前成为可用 target。

系统 MUST 先完成来源校验、固定缓冲磁盘复制/所选正文依赖物化及资源处理，再发布新 Session main 或现有 Session 的新 child catalog。失败只删除本次 staging、释放本次资源预留，不把原文件正文读入内存用于回滚，也不修改已有 Session/thread。发布后崩溃只按原 journal 确认或收敛资源回执；目标已 deleting/tombstoned 时释放预留，不重建目标或报告可用。必需资源无法保留时不发布；明确不继承的可选资源保留 unavailable 原因。

若显式继承 Goal，只允许新 Session main，创建新 Goal ID、复制 objective 和显式选定预算、用量清零、初始 paused，不恢复活动 Job。child 不能覆盖所属 Session Goal。调试方案仅在显式选择且 owner 支持目标处理时继承，发布前核验冻结的方案与目标配置，不复制活进程、端口或活动指针；历史锚点不捏造其当时的方案状态。

#### Scenario: staging 失败不留下半个 thread

- **WHEN** 磁盘复制、正文依赖或必需资源处理在发布前失败
- **THEN** 新目标在 catalog、右侧侧边栏和历史入口均不可用；仅清理本操作 staging/预留，source 和已有 target 容器保持原状

#### Scenario: 发布后恢复不重建被删除目标

- **WHEN** target 已发布但资源回执尚未终结，随后进程退出或目标被删除
- **THEN** 恢复按原 journal 和目标 fence 定点确认或释放，不重复导入、不复活目标、不以响应丢失推断未执行

### Requirement: Fork 测试 fixture 对单次操作施加固定期限

所有触发 fork 的测试 fixture SHALL 对单次 fork 操作施加固定 60 秒的单调绝对期限：浏览器场景从用户确认提交开始，API 场景从发出请求开始，直接服务调用从进入调用开始；fixture setup、弹窗中的用户思考时间和 fork 完成后的独立聊天不计入。操作完成以服务端可证明目标已 durable/published 的完成结果为准；跨 Gateway 场景中 `pending` 回执不算完成，必须等到 owner 确认。

同一次未完成操作的 HTTP 请求、phase wait、重试和恢复 MUST 共用最初的单调 deadline，不能在每个阶段、连接或重试时重新获得 60 秒。目标已完成后发起的独立幂等验证属于新操作，重新取得固定 60 秒。单次预算不可禁用或延长，测试套件总预算独立管理。

达到 deadline 时测试 MUST 明确失败并保留包含 target、operation/key、最后阶段、elapsed 与 `backend_state=unknown` 的诊断；不得据客户端超时推断后端已经停止、取消或回滚。为覆盖阻塞事件循环的同步工作，fixture 的外部 watchdog MUST 独立于被测进程观察同一个绝对 deadline，在写入并持久化该诊断后终止当前隔离测试运行；不得引入生产 deadline 协议或伪造失败后的成功结果。

耗时或性能优化短期不能完成时，实施 MAY 按用户裁定保留具名 TODO 并继续后续功能，记录原始命令、结果和未验证范围，等待其它功能完成后人工优化。对其它 OpenSpec 依赖不大的独立复杂边缘流程 MAY 采用相同延期方式，保留原验收与断言，待主体功能完成后人工处理。延期 MUST NOT 放宽本 requirement 的测试保护或把失败记为通过，也不豁免历史保真、目标/资源归属、授权、并发一致性及发布恢复合同。

#### Scenario: 一次 fork 的重试和恢复共用 deadline

- **WHEN** 同一次 fork 在初次请求、phase wait 或恢复请求中耗尽 60 秒
- **THEN** 测试明确失败并记录 `backend_state=unknown`，不能重置预算或将后端继续运行误报为已取消；若在 deadline 内恢复完成，则只以 durable/published 结果判定成功

#### Scenario: 已完成操作的幂等验证重新计时

- **WHEN** 一次 fork 已得到 durable/published 完成结果，随后测试独立重放同一幂等请求核验结果身份
- **THEN** 重放拥有新的固定 60 秒期限，原操作创建时间不会使它立即过期

### Requirement: Compaction 创建 SQLite context view

系统 SHALL将compaction表达为SQLite中新的context view和控制事件，不创建新的JSONL segment或控制记录。摘要和保留的canonical item继续作为不可变item追加到rollout.jsonl，新的view通过item range/reference选择摘要和保留范围。实际compaction是source/root重建的prefix epoch边界；compaction事务提交view及`PendingPrefixEpochTransition(reason=compaction)`，但pending记录没有wire bytes且不是applied epoch。下一次真正model-call preparation可以精确复用source base/delta，也可以从activation coordinator冻结的ResourceRegistry内存snapshot取得tracked published revision并物化为完整source，然后把source decision、首个新epoch assembly seal与transition消费原子提交。仅为移除已处理 head-internal notice refs 的`runtime_notice_consumed` transition必须复用同一 transition/applied owner，不能当作compaction/rewind source boundary；与compaction同 seal 时由该 owner保留两个reason事实并原子提交。source restore/fork/copy consumers MUST 过滤 `runtime_notice_consumed`，不从其恢复source checkpoint或继承fork lineage。snapshot不可用时保留compaction view/transition但不产生半assembly或dispatch，且不读取文件、网络或其它provider。所有第一条真实用户消息后的summary/source完整或增量内容必须保持独立user-role item。source/detail失效不能被解释为物化理由来修复旧sealed assembly；精确恢复仍显式失败。

#### Scenario: 压缩后继续运行

- **WHEN** compaction 成功提交并开始下一轮 Agent 执行
- **THEN** 新checkpoint引用包含摘要和必要尾部item的SQLite context view并登记pending compaction transition，后续item继续追加到同一个rollout.jsonl；下一次model-call preparation若精确复用source overlay或从冻结Registry activation snapshot物化tracked published完整user-role revision，都必须与首个新epochassembly及transition消费原子提交，不得修改旧item、请求期读源或用当前文件修复旧assembly

#### Scenario: 压缩后读取原始历史

- **WHEN** 调用方请求 compaction 之前仍未被 pruning 的历史
- **THEN** reader 通过 SQLite 旧 view 读取原始 item，且不把 compaction 控制事件作为模型请求 item 或用户消息

#### Scenario: compaction 前的 source overlay 保留审计 lineage

- **WHEN** `AGENTS.md` 或已应用的 skill source 在 compaction 前有 base A 和 A→B 增量
- **THEN** compaction的新epoch可以继续精确引用A与A→B，也可以按明确materialization policy从冻结Registry activation snapshot取得并引用B的完整revision；仅owner声明`root_eligible`且实际新epoch重建时可合并唯一system root，`tail_only`仍为独立user-role item。A与A→B的item/provenance保持可审计，不被物理覆盖或删除；source/detail失效作为旧assembly明确错误，不能触发静默重建或当前源读取

### Requirement: Rewind 在 SQLite 中创建新 branch 和 view

系统 SHALL在同一个rollout.jsonl上追加后续canonical item，并在SQLite中创建新的branch、context view和control event。rewind事务改变`history_view_revision`和canonical history可见尾部，恢复checkpoint-versioned tracking state并提交`PendingPrefixEpochTransition(reason=rewind)`；旧item保持不可变，旧后缀从新view中隐藏。pending记录没有wire bytes且不是applied epoch。目标checkpoint若保留tracked registration但最新已注入revision不在重建后的active view，下一次真正model-call preparation必须从activation coordinator冻结的ResourceRegistry内存snapshot取得published完整revision，并把恢复事实、新source overlay epoch、首个rewind epoch assembly与transition消费原子提交；合法新epoch只把owner声明`root_eligible`的受信有效状态合并唯一system root，`tail_only`仍独立user-role。snapshot不可用或seal失败时保留rewind view/transition但不产生半item或dispatch，且不得读当前源。snapshot/untracked不自动恢复。不得从overlay lineage把cutoff后的delta直接复活，不得创建semantic segment、parent segment或复制旧item正文来伪造新branch。

#### Scenario: 只回退不继续

- **WHEN** 用户将 active head 回退到历史 checkpoint 或 item anchor，但暂不发送新消息
- **THEN** 系统切换到新的 branch/view，旧后缀仍可由旧 checkpoint/view 读取但不属于当前 view

#### Scenario: Rewind 后继续追加

- **WHEN** 用户 rewind 后执行 continue 或 resume
- **THEN** 新用户 item、tool call item 和模型输出 item 追加到同一个 rollout.jsonl，并加入 rewind 创建的新 view

#### Scenario: Rewind 跨越旧后缀

- **WHEN** 当前物理尾部已经包含后续 item，但用户回退到更早 item anchor
- **THEN** 新 view 只引用目标边界的有效范围和新 item 范围，不把物理尾部旧后缀加入当前上下文

#### Scenario: rewind 到 source delta 之前

- **WHEN** source overlay 为 A base 加 A→B delta，而 rewind 的 history view 不再包含保存该 delta 的历史 item
- **THEN** 下一次model-call preparation以rewind view作为canonical history，并从冻结Registry activation snapshot取得tracked published revision B，把恢复事实、新source overlay epoch、首个rewind prefix epoch assembly及transition消费原子提交；`root_eligible`可合并新root，`tail_only`为独立user-role item，旧A→B只保留审计lineage，snapshot/untracked不恢复，当前源不被读取

### Requirement: Fork 保留 checkpoint-level pending state

fork SHALL 如实保留复制范围内历史 checkpoint 的 channel、pending_sends 和 pending_writes 作为冻结历史事实，包括 task path、顺序、serializer、BLOB和hash。`full_rollout_copy` 保留完整快照内的历史 checkpoint；`view_prefix_fork` 及其 `context_fork` 入口只保留正文依赖在所选成员及必需 backing 中闭合的历史 checkpoint，不为保留引用已排除正文的 checkpoint 扩大前缀或展开摘要的 provenance。未保留的来源 checkpoint ID 仍可作为 copy-origin 审计事实，不得宣称其原快照在目标可查询。

历史 pending state 与目标当前执行 head MUST 分离；目标读取历史时不恢复源任务，当前续聊不 dispatch 旧 pending tools。前缀目标的新 head 必须在 staging 的同一事务中、裁剪来源 checkpoint 前，由唯一 checkpoint owner 从精确 seed 原样继承需要的非 messages channel 值、版本和序列化事实，messages 绑定目标有效 selection，排除明确属于源执行的临时控制 channel，并以新的 checkpoint identity 保存空活动 pending state。新 head 不是原 seed 的历史副本，不改写原 checkpoint 字节；安装与恢复共用同一内核。目标首次执行创建新的 execution/checkpoint，不以清空或改写保留的历史记录实现隔离。

#### Scenario: 历史 pending state 不启动旧任务

- **WHEN** 复制范围内正文依赖闭合的 source checkpoint 包含 pending sends/writes，target 读取该历史并开始新聊天
- **THEN** 历史pending字节、顺序与hash保持可核验，目标当前运行状态不领取或发送这些旧任务，新执行只消费目标新准入

#### Scenario: 前缀排除 seed 的旧正文

- **WHEN** 精确 seed 的 messages 或 pending 引用了前缀排除的正文，但其非消息 channel 是目标后继所需状态
- **THEN** staging 先从完整 seed 构造独立新 head，再移除正文依赖不闭合的原历史 checkpoint；新 head 保留所需非消息值和版本、只消费目标 selection 且没有旧活动 pending，不为保住原 seed 展开全部历史正文

### Requirement: Fork anchor 具有明确包含语义

系统 SHALL 区分 `inclusive` 和 `before` 两种 item anchor。context view/reference 必须保存实际选择的逻辑 item 范围，Turn anchor 解析 MUST 使用 `TurnRecord.root_input_item_id` 作为用户交互起点，不得把 item 边界丢失或隐式扩大到物理尾部。

#### Scenario: Inclusive fork

- **WHEN** 用户从 B:3 创建 inclusive context fork
- **THEN** 子 view 包含截至 B:3 的有效 item，不包含 B:3 之后的旧后缀

#### Scenario: Before replay

- **WHEN** 用户编辑并重新执行 B:3
- **THEN** 新 view 只包含 B:3 之前的 item，再追加编辑后的 B:3 item 和新的后续 item

### Requirement: 用户操作使用 Turn anchor，compaction 保留 Message anchor

系统 SHALL 继续允许用户可见的 rewind、replay 以及从历史 Turn 发起的上下文操作使用稳定的 `turn_id` 和 `inclusive`/`before` 语义；backend resolver MUST 通过 `RolloutCheckpointSaver` 读取已提交的 active-lineage source view，并解析到 `TurnRecord.root_input_item_id` 对应的 canonical item 起点，再在需要时解析到 `content_part_id`/fragment anchor。业务层和 projector 不得直接扫描 RolloutStorage、AppendWriter 或内部 context reader。内部 compaction、fork 和恢复流程可以直接使用细粒度 durable item anchor；interrupt 首先使用当前 stream 的内存 cursor/ItemDraft，只有需要跨重启恢复或审计时才保存对应的 item/content-part reference。request-only context reference 与 pending runtime notice 不属于 view，不能直接作为 durable operation anchor。Turn 入口由后端解析精确 view；消息 fork 入口 MUST 固定点击 item、所属 view 与修订，不传真实 offset 或物理 message 序号。

任何将被继续执行或编译为 Provider request 的目标 view MUST 在 tool protocol 上闭合。若解析后的 item/content-part anchor 位于 assistant tool-call group 与任一匹配 terminal tool result 之间，resolver MUST 返回 `tool-protocol-boundary-conflict`，保持原 view 且不得创建新 view、branch、checkpoint 或 prefix epoch；响应可以提供不含内容正文的前后最近安全 anchor，但不得自动移动用户选择、合成 result 或按物理序号猜测边界。read-only history 可以保留 partial/incomplete group 用于审计，但不得把它标记为可执行。

系统 SHALL 保留 item 级定位能力。自动 compaction、用户主动 compaction、重放和 durable interrupt finalization 均可以使用 canonical item/content-part 作为边界；不得因为 Turn 或 LangChain message projection 存在而把操作强制对齐到 message 末尾。实时 interrupt 可以只停留在内存 cursor，不能因此要求每个 raw chunk 都成为 SQLite anchor。所有 durable anchor MUST 明确记录 `inclusive`/`before`、source view/branch 和可恢复性；content-part anchor 至少需要稳定 part identity、ordinal、hash/prefix hash 和 recovery capability，任意字符/token offset 不得单独作为 durable anchor。

#### Scenario: Turn 操作解析到 tool call 之前

- **WHEN** 用户从包含 reasoning、assistant output 和 tool call 的 Turn 发起 before replay
- **THEN** resolver 将 Turn 入口解析为 tool call item 之前的精确 anchor，保留前置 item，不构造或修改 LangChain message 来表达边界

#### Scenario: content part 中断

- **WHEN** stream 在 assistant_output item 的某个 draft/content part 后被用户中断
- **THEN** finalization 保存 partial 状态；只有跨重启恢复或审计需要时才保存 item/content-part 停止 reference，恢复不会把该事实扩展为完整 assistant message

#### Scenario: item anchor 不属于 source view

- **WHEN** 调用方提交的 item/content-part anchor 不在目标 branch/view 的有效 lineage 中
- **THEN** 系统返回明确的 stale/unreachable anchor 错误，不按最大 message sequence 或最新 checkpoint 静默替换边界

#### Scenario: item anchor 切入 tool 调用与结果之间

- **WHEN** rewind、replay、compaction 或 fork 将一个 assistant tool-call group 与其任一匹配 terminal tool result 分隔到 active view 边界两侧
- **THEN** 系统返回 `tool-protocol-boundary-conflict`，不改变原 view、不创建目标，并返回可选择的安全 anchor metadata；只有只读历史可以展示该 partial group，执行投影不得消费它

#### Scenario: system reminder 位于新 Turn 之前

- **WHEN** source view 中最后一个执行因中断产生 pending runtime notice，随后用户提交新的普通输入
- **THEN** resolver 将新输入的 `root_input_item_id` 作为新 Turn 起点；pending notice 只按其 `notice_for`/assembly 关系参与请求，不因其 wire role 或物理位置取代新 Turn root

## ADDED Requirements

### Requirement: rewind、compaction、fork 与 delegated thread 的边界不可混同

rewind、compaction 与 checkpoint branch MUST 只重建其 selected `SessionThread` 的 execution active view、source state、ToolSet applied binding 和 graph checkpoint；不得影响同一 Session 的 sibling thread。`history_replay` 是同 owner 的只读历史投影，遵守下述历史 view 合同，不属于 execution active view 重建。fork 按显式目标创建新 Session main 或既有 Session 的新 child，保留历史 local ID并以目标归属解析；其创建范围由本 spec 的 fork 合同定义。新 delegated execution 则在 parent Session 创建 child SessionThread，不得用 context fork 或新的 product Session 伪装。

#### Scenario: child thread rewind 不影响 main thread

- **WHEN** delegated child thread 从某个 item anchor rewind
- **THEN** 只有该 child thread 新建或切换 active view；main thread 的 canonical prefix、assembly、ToolSet binding、checkpoint 和 Turn ordinal 保持不变

### Requirement: history replay 使用只读 view selector，不替换 execution head

`history_replay` MUST 在所选 `(session_id, thread_id, checkpoint_ns)` owner 下创建不可变历史 view，并返回显式 `history_view_id`。它复用所选 source Turn、root 和 ordinal；不得创建 execution、切换 namespace 的 active branch、改变 branch status/head 或恢复当前 CSM/source、ToolSet/applied epoch。历史 view 和其 control snapshot 归现有 view owner，不维护第二个 execution head。

`get_tuple` 使用该 selector 时 MUST 校验 owner 和 `history_replay` view 类型后只读投影；`put` 或其它执行写入使用历史 selector 时 MUST 在任何持久化前明确拒绝。普通执行调用仍解析唯一 execution active branch/head。已有 rewind/compaction pending transition 的 target view、branch 和完整 payload 不得因历史读取改变或被重绑。`runtime_notice_consumed` 不是 seal 前持久化的 pending transition：同一接受 Turn 的 preparation 只可从该 Turn 冻结的 `RuntimeNoticeAcceptanceRef` 在内存中构造消费候选，不得将候选写成可由其它 owner 接管的 pending 状态；若该候选使本次 assembly 新增被排除的 selected ref，首个成功 seal 才在同一 SQLite 事务写入 consumption transition、acceptance ref、assembly 与 epoch。若没有新增排除 ref，则仍引用本次 acceptance，沿用已应用 epoch，不创建 consumption transition。消费事实和 `RuntimeNoticeAcceptanceRef` 归原接受 Turn，不得由 `history_replay` 重绑或转交后续 acceptance。`runtime_notice_consumed` 不作为 source restore/fork lineage boundary。

#### Scenario: 历史读取不替换当前执行状态

- **WHEN** 同 owner 已有 rewind/compaction pending transition，或已有与接受 Turn 绑定的 `runtime_notice_consumed` transition，调用方创建 `history_replay` 并用返回的 `history_view_id` 执行 `get_tuple`
- **THEN** 返回所选历史 projection，原 Turn/root/ordinal 复用且不新增 execution/model-call/dispatch；execution active branch/head、当前 CSM、ToolSet/applied epoch、rewind/compaction pending transition 和已提交 notice consumption fact 均保持不变，notice consumption 不触发 source restore

#### Scenario: 历史 selector 拒绝写入而新 Turn 可以继续

- **WHEN** 调用方对历史 selector 执行 `put`，或随后通过正式 acceptance 创建新的真实 Turn
- **THEN** 历史写入明确失败且没有持久化副作用；新 Turn 使用原 execution branch 的独立 root/acceptance/execution identity，只能从自身冻结的 acceptance selection 构造瞬时 notice-consumption candidate，并在自身首个成功 seal 中按原唯一 owner guard 原子提交；不得复用旧 Turn/execution 的消费候选或放宽 branch guard

### Requirement: Checkpoint 区分 canonical view 与 request-only assembly

checkpoint SHALL 将可恢复的 canonical context view、LangChain message projection 和某次请求的 `ContextAssemblySnapshot` 分开引用。checkpoint 可以记录当次请求应用过的 request-only prompt contribution 或独立 `ToolSetRef` 的 assembly reference、版本和 hash，但不得把合并后的 system/developer wire message、ToolSetSnapshot 或 tool definition 当作 canonical history 写回 view，也不得用当前 middleware 配置伪造旧 assembly。

checkpoint 在 restore/replay 时必须把已选 ToolSetRef manifest identity（snapshot id、source revision、length、hash token、schema/policy version 和绑定 policy）作为 `context-plan-hash:v2` 的一部分逐字段校验；工具 schema/config 或 policy 变化只能得到新的 plan/hash，不能从当前 registry 静默重建旧工具集合。`ContextContribution.contribution_kind=tool_set` 非法，旧值只能进入 legacy quarantine，不能成为 checkpoint 中的 Provider tool definition。

#### Scenario: checkpoint 恢复动态 system context

- **WHEN** 某次执行使用了 skill 说明或 workspace/environment prompt，但这些内容没有被提升为 canonical item
- **THEN** checkpoint 恢复 canonical view 时不新增历史 message；如需重放原请求，系统通过保存的 assembly/request-only reference 判断是否可以精确重建或明确报告 source mismatch

#### Scenario: checkpoint 恢复持久化系统通知

- **WHEN** 中断提醒或 compaction notice 已通过 append intent 成为 canonical system notice
- **THEN** checkpoint 的 view 可以引用该 canonical `runtime_notice` item；若它位于第一条真实用户消息之后，当次请求只能按plan ordinal投影为独立user wire item，并与canonical identity保持可追踪但不互相替代，不得提升为中途system

### Requirement: Turn root、execution 和 finalization 在分支操作中保持稳定

分支操作必须使用统一的 `Turn.status` 闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`。合法转移为 `open -> active|cancelled|failed|unknown`、`active -> completed|completed_empty|interrupted|cancelled|failed|unknown`；只有显式 `resume_turn` 才允许 `interrupted -> active`，以及 reason=`execution_lost` 的 `unknown -> active`，且必须创建新的 execution。其它 terminal outcome 不可转移。`completed_empty` 是唯一的无 canonical output 正常终态名称；普通 `cancelled` 是吸收态；fork imported history 另由快照归属策略标记不可恢复，不改写原状态，两者，不能执行原 Turn 的 `resume_turn` 或 `dispatch_replay`，必须返回 `turn_not_resumable`；新执行只能通过独立的 `replay_as_new_turn` 创建新的 Turn。

Turn.status 转移表冻结为：

| 当前状态 | 允许的下一状态 | 条件 |
|---|---|---|
| `open` | `active`, `cancelled`, `failed`, `unknown` | acceptance 已提交后由执行启动或明确控制结果收敛 |
| `active` | `completed`, `completed_empty`, `interrupted`, `cancelled`, `failed`, `unknown` | terminal convergence 一次提交 |
| `interrupted` | `active` | 仅显式 resume，必须新建 `execution_id` |
| `unknown` | `active` | 仅 reason=`execution_lost` 的显式 resume，必须新建 `execution_id` |
| `completed`, `completed_empty`, `cancelled`, `failed` | 无 | terminal；对原 Turn 的 `resume_turn` 或绑定原 `turn_id` 的 `dispatch_replay` 不发生状态转移；`cancelled` 均返回 `turn_not_resumable`，新执行只能由独立 `replay_as_new_turn` 创建新 Turn |

回放操作必须分离：`history_replay` 只生成 projection，不创建 execution；`resume_turn` 只按上表复用允许恢复的原 Turn；`dispatch_replay` 表示把 Provider dispatch 绑定到原 `turn_id`，对普通 `cancelled` 和所有 fork imported historical execution 都返回 `turn_not_resumable`；`replay_as_new_turn` 才是显式的新 Turn 创建操作，创建新的 Turn/root/acceptance/initial execution 并以 `replay_of_turn_id` 保存 lineage。`replay_as_new_turn` 后续可以进行 Provider dispatch，但不是原 Turn 的 `dispatch_replay`，同一 API 语义不得同时返回错误并创建新 Turn。fork imported history 不允许任何同 Turn resume 或 dispatch replay；快照不因该拒绝重写源历史状态。

checkpoint、branch 和 view SHALL 保存 Turn 的 `turn_id`、owner-thread-global 且不可重排的 `turn_ordinal`、不可变 origin `source_branch_id`、`accepted_ingress_id`、`acceptance_idempotency_key`、`root_input_item_id`、execution/model-call lineage 和 `final_item_id` 引用。在同一 `(session_id, thread_id)` owner namespace 内，派生 branch/view 只能复制既有 Turn/item 的引用，不得复制或重新编号 TurnRecord；无新用户输入的 continue/resume 只有在 Turn.status 转移表允许时才继续原 Turn并创建新的 execution/model-call identity。`history_replay` 是唯一可以复用 source Turn/root 的历史 projection，不创建 execution；显式 `replay_as_new_turn` 必须创建新的 Turn/root/acceptance/initial execution，并在 active view 登记新的 `logical_turn_ordinal`，source history 可以作为前缀复制或引用但 source Turn/root 不能成为新 Turn 的 root，并用 `replay_of_turn_id` 关联。跨 owner fork MUST 保留历史 local ID、root、业务序号和 acceptance 事实，以目标资源根及冻结 namespace binding 区分全局归属；不逐项 remap 或重算历史 admission/hash。复制历史不可恢复，fork 后新输入 MUST 在目标选定 thread 创建新的 Turn/root/acceptance/execution 和当前 branch；新业务序号不得与已导入历史冲突。retry/resume 不得复用已经提交的 output item identity；没有成功 finalization 时不得以最后一个 assistant item 替代 `final_item_id`。`context_view_turns.logical_turn_ordinal` 是 view-local 唯一顺序，必须和 owner-thread-global `turn_ordinal` 分开；上述新 Turn 操作不能被解释为绑定原 Turn 的 `dispatch_replay`。

#### Scenario: resume 不创建伪造 Turn

- **WHEN** 用户从被中断的 execution 执行 continue/resume，且没有提交新的用户输入
- **THEN** 新 branch/view 可以继续原 Turn 的 root，并记录新的 execution/model-call lineage，不创建第二个 `TurnRecord`、`root_input_item_id` 或全局 `turn_ordinal`

#### Scenario: 新用户输入创建新 root

- **WHEN** pending runtime notice 之后提交一条普通用户输入
- **THEN** 系统创建新的 `turn_id`、全局递增的 `turn_ordinal`、新的 root input item 和新的 origin `source_branch_id`；pending notice 只能通过 ambient/assembly relation 参与请求，不改变新 Turn 的 root

#### Scenario: 同一 owner thread 内 history_replay 复用 Turn 但不隐式执行

- **WHEN** 从已有 Turn 的 inclusive 或 before anchor 在同一 `(session_id, thread_id)` 内创建 branch/view
- **THEN** 新 history view 复用原 `turn_id`、`turn_ordinal`、`source_branch_id` 和 `root_input_item_id`，并为该 view 登记自己的 `logical_turn_ordinal`；复制或加载 view 不产生新的 execution，`history_replay` 也不产生 execution，只有符合 Turn.status 转移表的显式 `resume_turn` 才新增 execution/model-call lineage

#### Scenario: 同一 owner thread 内 replay_as_new_turn 创建新 active Turn

- **WHEN** 从已有 Turn 的 inclusive 或 before anchor 在同一 `(session_id, thread_id)` 内明确选择 `replay_as_new_turn`
- **THEN** 新 active view 可以复制或引用 source history 作为上下文前缀，但必须登记新的 target-local `turn_id`、新的 `user_input` root、acceptance、initial execution 和新的 view-local `logical_turn_ordinal`；source Turn/root 仅作为前缀或 lineage，不能成为新 Turn 的 root
- **AND** 新 Turn 的 Provider dispatch 绑定新 `turn_id`，该操作不是 source Turn 的 `history_replay`、`resume_turn` 或 `dispatch_replay`

#### Scenario: 跨 owner fork 保留 local ID 并创建新归属

- **WHEN** 从 source thread 通过任一 fork 范围创建新的 target main 或 child
- **THEN** 历史 local ID、root/acceptance、时间及 hash 保留，读取只落到目标本地 snapshot；target 的全局身份由新归属确定，旧执行不可恢复，新输入或 replay_as_new_turn 才创建新身份

#### Scenario: root lookup 与 view 顺序分离

- **WHEN** 同一 Turn 出现在两个不同 fork lineage 的 view 中
- **THEN** resolver 先按 view lineage 定位 `context_view_turns`，再解析同一个 `root_input_item_id`；每个 view 可有不同 `logical_turn_ordinal`，不得按 `MIN(item_sequence)`、wire role 或 view 第一条 item 创建第二个 root

### Requirement: 跨 Session copy 必须冻结 source 后再写 target

fork SHALL 在来源端固定精确 thread/view/checkpoint revision、SQLite 一致快照、JSONL committed byte 上界、正文文件清单及必要资源引用。source read guard 只覆盖来源捕获；来源 snapshot 完整发布后释放，后续只消费冻结文件/manifest，不回读当前 source、隔离节点或磁盘扫描补齐。删除先关闭 source 准入时捕获失败；捕获先行时删除等待 guard 释放。

完整快照 MUST 使用 SQLite 一致磁盘快照及分块文件复制；不全量解析历史 JSON、不累计全部 item、relations 或 identity mapping、不逐项重算业务 hash。允许分块字节完整性校验。正文内存 MUST 由固定缓冲区和有界批次控制，不随历史正文大小增长；target 第一次历史读取和发送也必须先按索引与输入预算选择，再读取所需正文，不能把全量加载推迟到首次发送。

target 创建/发布与 source gate/guard 阶段 MUST 分离；同 Session 两个 thread 复用同一 Session gate并按阶段使用，不递归取得两个 gate。跨端由 source owner 导出冻结 artifact、target owner 流式接收并 staging/发布，复用统一 Gateway 路由，不把远端业务库真实路径或数据库连接交给调用端。source/target 任一失败按原 operation 和回执恢复，禁止双端同时持锁或事务。

workspace 共享附件正文 MUST NOT 进入 source/thread staging 重复复制。仅当统一资源目标处理结果要求 target 保活或导入时，才由附件 owner 建立精确 operation 绑定的 claim/预留；已有独立 workspace 生命周期的可读引用不建立多余 copy claim。claim 在释放 source guard 前完成必要保活，target catalog 发布前不提前授权预留引用。

资源保留/导入必须继续兑现既有 attachment claim/settlement 的删除与 GC 合同：需要 claim 的操作预登记 claim ID/hash 与 publication preimage，状态为非终态 `preparing|published_pending_attachment_commit` 或终态 `committed|aborted|target_deleted`；finalizer 与 target 删除竞争目标 gate。finalizer 先行则完成保留并进入 committed，删除按正常引用释放；删除先行则释放 reserved 或已 committed 的精确 target 引用并进入 target_deleted。未发布失败只释放本 operation 预留。无 claim 的复用不创建 settlement record；不扫描 digest 补 claim、不复用其它操作 claim、不在非终态 record 下隔离目标、不让恢复复活 target。

#### Scenario: source capture 与删除双顺序

- **WHEN** capture 先取得 source read guard，或删除先关闭 source fence
- **THEN** 前者完成冻结后释放 guard，source 可删除而 target 只读 snapshot；后者明确失败且零可见目标，不从隔离节点补读

#### Scenario: 源并发追加不污染快照

- **WHEN** source 在冻结 SQLite revision 和 committed offsets 后新增 item 或切换 view
- **THEN** target 只消费原冻结范围；数据库、JSONL 和 detail 不混用不同修订，旧 bytes/hash 不变

#### Scenario: 磁盘快照复制与后继读取有界

- **WHEN** 对已提交历史进行完整复制、删除独立 source 并在 target 读取历史及发送新消息
- **THEN** 复制使用固定缓冲和有界批次，无全量 JSON 解析、item/relations 累积或逐项 remap；历史字节/hash 保持，后继读取和首次请求按索引与输入预算选择正文，失败 staging 可定点删除；验收不设固定数据体积或专门的大数据峰值内存门

#### Scenario: 复用附件不复制字节且不留 GC 空窗

- **WHEN** 同工作区 target 复用现有 blob，source 引用随后释放且 GC 与 target 发布并发
- **THEN** blob 正文和稳定内容身份不重复生成；owner 按实际生命周期已有保留或新增目标保活保证可读，预留不提前授权，删除/恢复仅处理本操作引用

#### Scenario: 跨端中断与目标删除按原回执收敛

- **WHEN** 远端传输中断、发布响应丢失，或资源 finalizer 与 target 删除竞争
- **THEN** 未发布 staging 清理；已发布目标按既有 operation 查询真实结果，删除先行时释放精确预留且不得复活；无双端 gate/事务，无本地同名目标回退

### Requirement: Fork 必须提前解析正文引用并区分来源审计

`view_prefix_fork` SHALL 在发布前沿 typed 正文引用读取依赖，固定引用的完整 owner、local ID、revision/hash/length，并在目标本地形成独立可读的正文集合。依赖可以位于所选 view 可见前缀之外，但其存在不自动增加可见成员或模型输入；顺序仍由选中 view 的 membership/selection 决定。base/delta、multipart、tool protocol 与所选 assembly 所需 detail 按各 owner 合同核验；缺失、版本漂移或越权明确失败。

引用遍历 MUST 去重、防环并按索引、有界批次和固定缓冲执行，不把所有引用正文同时保存在内存。摘要的 provenance、纯 lineage/audit 只保存事实，不无限展开被摘要替代的历史；正文中的普通 URI 字符串不当作 typed 依赖，不扫描替换。附件或其它资源引用消费统一目标处理入口，不因此隐式激活技能、恢复 Goal 或访问源会话。

#### Scenario: view 外正文依赖可在目标本地解析

- **WHEN** 选中 item 引用同 view 外的正文，且该正文又有必要 typed 引用
- **THEN** 发布前解析并保留所需依赖，重复依赖只物化一次；源 view 清理后目标仍可读，依赖不自动成为额外可见消息

#### Scenario: 摘要来源不复活完整旧上下文

- **WHEN** 摘要 S 只以 provenance 指向大量已压缩 item
- **THEN** target 继承 S 和选中成员，不读取或加入所有被摘要历史；缺失的必需正文引用与可保留的审计来源有不同结果

### Requirement: Fork 来源与目标必须使用统一资源 URI

fork SHALL 用资源 URI 定位 source thread 及 target 容器，用并列结构化字段表达 view/revision、anchor、范围、资源继承和 `new_session_main|new_child_thread` 创建意图。source Session 入口明确规范化为其 catalog main thread；显式 child 来源不得回退 main。target 不存在时 URI 指向已有 Workspace/Session 容器，发布后返回新目标的完整身份与 URI；不覆盖、合并或恢复既有 thread。既有生成策略明确要求新 Session 时 MUST 显式传 new_session_main，不受消息 UI 的 subthread 默认值影响；委派仍按既定 seed/任务合同创建 child，不因此隐式继承整条 main history。

URI 的 kind/模板及规范化只由统一寻址 owner 登记；Session/Thread/Workspace 容器均为领域目标，不伪装为文件目录。目标固定 Gateway、Workspace 和目标 Session，不依赖当前激活工作区。当 `new_child_thread` 的目标 URI 是某个已存在 Thread 时，owner 必须核验该 Thread 属于 URI 中的 Session；该 Thread 只用于定位目标 Session，不建立新 Thread 的 parent-thread 关系。source URI 独立保留来源 Thread 与 fork context lineage；不得从目标 URI 推导 source lineage 或目标 parent-thread。跨端操作要求 source 读取/导出和 target 创建/导入的对应授权；send/read/wait grant 不能升级为 fork 授权。不可达、不支持或目标 fence 已关闭时明确失败，无本地同名回退。

发布回执 MUST 返回由创建 intent 冻结的 `target_gateway_id`，以及 `session.workspace_id`、`session.session_id`、`thread_id` 和对应 URI。接收方后端使用统一 URI grammar 核验 URI 与固定目标和发布身份一致；客户端消费结构化身份匹配已登记的 Gateway/Workspace 路由，不自行拆解 URI 推导路由。缺少身份、目标未登记或登记不唯一时明确报错，不使用当前激活目标补全。

#### Scenario: 同 Session 默认目标是新 subthread

- **WHEN** 用户未修改目标，从当前 main 或 child 的消息底部发起 fork
- **THEN** 在该来源所属 Session 创建新 child，来源关系指向精确 source thread；不新建导航子 Session，不复用旧 child，不自动执行或回报

#### Scenario: 本地和跨端目标共用创建意图

- **WHEN** 用户显式选择本地/远端 Workspace 的新 Session main，或已有 Session 的新 child
- **THEN** 同一 fork 合同在指定目标端 staging 和发布，返回真实新目标 URI；目标不可达、未授权或不支持时明确失败，不改变默认工作区

### Requirement: Fork 引用资源必须按统一目标处理合同复用或迁移

fork SHALL 具名消费 `add-unified-virtual-resource-addressing` requirement「资源目标处理必须复用 owner 事实并单点决策」。附件、工具输出、detail、技能来源及可选业务资源都走同一检查入口；生命周期与实际副作用仍由其领域 owner 执行，不按模式名维护附件/调试等平行复制矩阵。

默认继承所选上下文正文，不额外复制资源；同一权威工作区存储域内可读的共享资源按属性复用，不把不复制误报为不可用。只有生命周期需要时建立目标保活；显式选中的跨域资源按目标能力导入或复用目标已有内容。未选择跨域附件等额外资源时保留历史引用并明确 unavailable 原因，不借 source URI在线补读。运行必需正文不能按可选附件遗漏处理。

#### Scenario: 同工作区共享附件默认复用

- **WHEN** 用户保持默认继承选项，同工作区 source item 引用已有共享附件
- **THEN** target 跳过正文复制和逐项身份重造，由 owner 核验可读性及必要保活；原正文引用可展示可用，不恢复其它资源或运行态

#### Scenario: 跨域附件未选择继承

- **WHEN** target 位于不同存储域且用户未选附件继承
- **THEN** target 仅保留正文和历史附件引用，明确标记未继承，不下载附件、不读取源 URI、不伪造可用目标引用

### Requirement: 消息 fork 弹窗与侧边会话共用创建机制

消息底部 fork 按钮 SHALL 打开包含来源、目标、继承选项的界面。默认绑定点击 item 所属的精确 source view/revision，以 `inclusive` 的 `view_prefix_fork` 截止该 item，默认 target 为该 Session 新 subthread，默认不额外复制资源/Goal/调试或运行态。界面显示来源、截止消息和目标，URI 可在定位详情中查看；不能默认扩大到完整 Turn 或最新消息。

确认时 MUST 校验原绑定；源新增消息或切换激活工作区不能改变本次范围和目标。协议不完整时展示错误和可选择安全锚点，不自动移动。创建成功后默认在主窗口右侧侧边栏打开该新 subthread，保留主会话选择且等待用户输入；关闭侧边对话只关闭展示。侧边会话新建、消息 fork 和已有 fork 创建入口消费同一合同，打开已有 thread 不再次 fork。显式创建独立 Session 或远端目标时按返回的精确归属导航，不将其误挂入当前 Session。

#### Scenario: 消息按钮默认创建可聊天侧边分支

- **WHEN** 用户点击已提交消息的 fork 按钮并接受默认值
- **THEN** 弹窗展示截至点击 item 的 view 前缀及当前 Session 新 subthread；确认后出现真实 catalog child，在右侧侧边栏可读继承正文并发送新消息，不启动旧执行或改动主线程聊天

#### Scenario: 选择未变而工作区切换

- **WHEN** 弹窗打开后用户切换工作区或源新增后续消息
- **THEN** 来源修订、点击锚点与目标保持原绑定；无法兑现时明确失败，后续消息不进入副本
