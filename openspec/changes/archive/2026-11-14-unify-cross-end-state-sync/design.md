## Context

动机与范围见 proposal.md。当前可复用的能力包括：

- `useSessionGoalController` 的请求合并、写入代际及失败重读；Goal 同时接受 HTTP 与 `goal_updated`／`goal_cleared` 更新。既有 revision 是设置记数，当前设置请求没有预期版本，不能称为已具备 CAS；该记数也不覆盖全部用量变化。现有 DTO 的 accounted_jobs 随 Job 累积，不适合直接作为小对象同步。
- `MessageRunAccepted` 已返回 message／Job／Turn／Thread 及 dispatch；pending 队列有 `snapshot_version`，取出后沿 Job 生命周期。前端本地提交与权威队列不是同一对象。
- 导航已有 operation 身份、持久 outbox、回执、catalog_revision 与事件水位；资源树另有会话摘要拼接比较与刷新触发，存在可收敛的协调逻辑。
- Gateway 已有公开目录导出镜像、磁盘快照和 stale 搜索；普通业务读取仍走透明代理。前端目录分支主要是页面内存状态，不假设已有可跨启动恢复的完整目录缓存。
- 公开协议由 `proto/` 生成，既有 workspace observation 提供 SSE；不另建传输栈。代码仍在并发开发，实施各切片前需核对当时 owner 与调用方。

## 现有 owner 与接入边界

Goal 的业务控制 owner 是 `SessionGoalService`，由会话 API 在 `session_write_admission` 内调用；持久事实归 `PersistentTaskStore` 复用的 `persistent_tasks` 与不可变定义表，SQL 访问归 `TaskGoals`。内部累计用量和续跑去重由 `app/services/business/persistent_tasks/goal.py`、`app/services/orchestration/persistent_tasks/goal.py` 消费；Goal 更新与清空通知沿现有 `JobEventBus`、`GoalUpdatedPayload`／`GoalClearedPayload` 和 workspace observation 发送，不另建通道。HTTP 入口为 `app/api/sessions.py` 的 GET／PUT／DELETE `/sessions/{session_id}/goal`。

公开传输唯一来源是 `proto/boxteam/workspace/v2/public.proto` 的 Goal 展示、快照、命令与操作回执，通过 `scripts/generate_protocol.mjs` 生成 Python、Web、workspace-service 绑定及 OpenAPI/SSE 运行校验。真实 Web 消费者为 `src/clients/web/src/api/session/sessionGoals.ts`、`useSessionGoalController`、`useTaskGoal`、`state/sessionGoal.ts` 和 Goal 更新事件解析。内部 `SessionGoalStateDTO.accounted_jobs` 留在原任务 owner，公开 `SessionGoalDTO` 不携带记账明细。

消息由 `SessionOrchestrator.create_message` 协调原 Session／Thread admission、附件持有和 `MessageService`；普通消息的 operation_id 直接作为原执行准入的 admission_idempotency_key。唯一去重事实是 Thread rollout SQLite 的 executions 与同事务 execution_queue，不新增操作表或 JSON 回执。`RolloutTurnsMixin` 在 BEGIN IMMEDIATE 内检查身份及完整输入 hash，`JobService` 在原调度锁内产生 dispatch 水位；原身份查询只读原执行，不重新派发。客户端 `messageSubmissions` 复用 browserViewStore，仅保留未交接输入，Composer 草稿与权威历史继续归原 owner。旧 SessionMessageIdempotencyStore 及无人消费的 create_and_run 链路迁齐后移除。

目录的权威版本、节点与导航操作分别由 `SessionCatalogStore`、`SessionCatalogReadService` 和原 navigation queue owner 持有；配置投影版本取 `ConfigService` 已发布 revision，并与 Provider 投影使用同一 publication lock。`GatewayCatalogSearchService` 继续持有派生镜像，普通 children／breadcrumb／指定 ID 读取沿原代理返回适用镜像或有界权威范围。前端 `useSessionResourceExplorer` 只保留已加载分支，原 navigation outbox 负责命令依赖与确认，不新建完整目录库。

共享 proto/generated 由同一写入者生成，Goal、消息和目录各自迁齐生产者及消费者。已归档的 `refine-workbench-layered-caches` 提供浏览器原存储 owner；Quick Access 继续消费原资源与导航入口，本变更不另造搜索或历史链路。

## Goals / Non-Goals

**Goals:**

- 把跨端正确性规则变成共享协议与可直接使用的最小实现；各业务只保留自己的状态转换、数据读取和持久化。
- 小对象默认完整替换，分层集合默认失效重读，大型流保留原 reader；让接入方有明确选择而不需要实现一个庞大接口。
- 通过 Goal、消息 FIFO、目录三条纵向切片证明可用；包括会话执行状态的展示收敛，不重构 Agent 执行引擎。
- 前后端交接可恢复，失败可见；普通请求不增加全量扫描或逐跳落盘等待。

**Non-Goals:**

- 全局 SyncManager／CacheManager、全业务数据库、通用调度引擎、CRDT、跨库分布式事务或 exactly-once 执行。
- 把所有领域改成事件溯源、为所有 GET 添加缓存、为每个操作建立永久完整快照账本。
- 首批重写消息正文、Trace 或请求日志 reader；只规定它们可接入的边界，不预建未使用 adapter。
- 新增离线业务执行、扩展队列出队后崩溃恢复、自动删除完成 Goal，或为未发布开发数据增加迁移。

## Decisions

### 1. 统一语义与小型原语，保留领域 API

协议基础类型放在现有 `proto/` 公共协议域并沿原工具生成，领域请求和响应引用所需部分；不提供接收任意 JSON 的万能命令接口。纯客户端的版本应用、读取合并／取消与操作对账放在现有 `src/clients/shared/core`，Web hook 只连接 API 和展示状态。后端 owner 各自实现提交事务，Gateway 保持代理和目录聚合职责。

先从 Goal 和消息链路提取真实重复原语，再接导航已有 driver；不先定义大量 strategy／registry／plugin 接口。原导航持久 outbox 的业务依赖与批处理仍属于导航，不强制迁给小对象。替代方案“每个 hook 各自补竞态”继续增加重复，“一个框架接管所有业务”会引入更多状态和间接调用，均不采用。

### 2. 同步元数据最小且含义明确

| 概念 | 合同与来源 |
|---|---|
| 资源归属 | 稳定 Gateway／工作区及实际需要的 Session／Thread／资源 ID；路由连接 ID只选择路径，不改变业务身份 |
| 同步版本 | 同一资源有效生命周期内单调的公开状态版本；版本可重置的资源需可区分代际，复用已有有效代际而非一律添加新 epoch |
| 写入前置版本 | 决定某命令可否应用的原子比较依据，按领域选择；Goal 控制版本与纯用量变化的同步版本分开，消息追加不强制套用覆盖写 CAS |
| 读取最低版本 | 回执已确认的该资源提交水位；确认读取必须达到它，不能用较旧镜像或 not-modified 完成确认；与缓存校验用的已有版本区分 |
| 覆盖范围 | 完整小对象、目录父节点和查询页、摘要或原流片段；分页 cursor 不等于完整集合版本 |
| 来源与新鲜度 | 权威结果或 Gateway 镜像；镜像版本、更新时间、最近权威确认及失败原因，不把缓存命中当成远端确认 |
| 命令身份 | 稳定 operation_id，与原领域 admission／idempotency 身份统一映射，不建立第二套并行去重键 |
| request_id | 每次网络请求的追踪身份，按现有 Gateway 合同跨跳透传；不是重试操作身份 |
| 事件游标 | 原事件流中的续读位置；不用业务对象版本推测事件位置，也不建立跨工作区全局序号 |

具体线上的字段沿生成协议确定，但上述语义为强制合同。相同版本但同范围数据不一致属于协议故障，明确报告；禁止暖读全量 hash 来弥补版本管理错误。响应身份不匹配、错误代际和实际载荷损坏直接拒绝；不额外防御性扫描其他历史。

### 3. 三种读取模式，共用失效与发布规则

| 模式 | 默认处理 | 本次接入 |
|---|---|---|
| 小对象 | GET完整有界展示对象；可在版本相同时省略正文；写入返回展示对象并替换；无需为每个对象单独建 SSE | Goal 摘要、会话执行状态，复用现有观察流 |
| 分页集合 | 保存已加载范围；事件使相关范围失效并合并刷新；版本变更拒绝旧 cursor | 会话目录、后端 pending 队列的既有摘要／详情 |
| 大型历史／流 | 保留原有界 reader、摘要详情和快照游标；共享归属、取消和恢复规则 | 本次不扩写 reader，只核对队列交接到现有历史／Job 的边界 |

同一资源的较旧结果不得发布。相同请求可合并；显式写后重读必须越过写前的在途结果。切换目标时撤销旧发布权，错误也不能写入另一个会话。保留可信旧展示时同时显示未确认／stale 状态，不将它当成本次请求成功。

Goal 的公开投影只包含身份、objective、状态、预算、累计用量、必要版本及时间等有界字段；accounted_jobs 及内部续跑／记账去重记录保留在原后端 owner，HTTP、回执和更新事件都不得内联该明细。投影按原权威状态生成，不另存第二份 Goal。内部类型与公开类型沿唯一生成协议拆清，迁齐依赖旧字段的消费者，不保留旧全量对象接口。该切片只消除普通同步的全账本传输，不顺带改造内部记账存储。

默认不建立可跨重启恢复的前端目录数据库；现有内存页先显，冷启动从 Gateway 已有镜像取得页。未来若明确需要跨启动预览，可按缓存生命周期合同接入，不影响本次正确性。

### 4. 接收、应用与执行分别确认

```text
本地操作：待提交 → 提交中 → 已接收／已应用 → 完成权威身份交接 → 清理本地记录
                      ↘ 结果未知 → 查询原操作或以原身份重试
                      ↘ 明确拒绝 → 显示错误并保留可编辑输入

领域状态：Goal active／paused／complete；消息 queued → Job状态；导航 queued → committed等
```

Goal 普通设置／清空在原 owner 同步提交，直接返回 applied、有界完整展示对象或带版本的缺失状态及其提交水位。消息 accepted 只表示输入与队列准入已提交；导航沿原回执区分接收与应用，committed 回执保留 committed_catalog_revision。前端不能仅根据 HTTP 200、某项从列表消失或 Gateway 收到请求推断业务成功。

原 owner 将最小去重结果与应用事实放在同一事务／既有可恢复提交协议中；不能先发 accepted 再异步补存，也不能单写新账本却未提交业务对象。重复操作先验证归属和载荷一致性，再返回原接收事实；旧回执中的业务对象不覆盖更晚的同步版本。需要最新业务状态时另取当前对象。

Goal 的设置、替换、暂停／恢复和清空使用逻辑 Goal 槽位的 control_revision。它在同一 Session 的 main Thread 范围内跨创建、替换及清空单调推进，Goal 不存在时仍能取得对应版本，避免清空后版本归零的 ABA。改变控制字段的用户／Agent 操作和预算强制状态转换均推进该版本；纯 tokens／耗时计数变化只推进公开 sync_version，不让持续记账导致编辑持续冲突。保留旧 revision 的既有记数语义，不把其未经验证地当作跨生命周期前置条件；优先复用原 owner 可用的提交序号，只有缺少相应事实才补必要的槽位版本。

客户端同一 Goal 槽位的命令串行发出；结果未知时先对账前一命令，不能让后续清空等操作越过它。后端先检查同身份已知结果，再在应用事务内比较 expected_control_revision；不相等返回 HTTP 409、当前有界状态及版本且不应用。冲突刷新镜像并保留编辑输入，取消本批依赖旧前置状态的后续自动提交，需用户明确重新提交；不得自动换身份、改前置版本后覆盖他人修改。新用户提交才取得新操作身份，同一原命令的网络重试保持身份和载荷不变。Agent 内部控制写入也参与同一原子版本推进，不能绕过这个边界；消息追加和导航继续采用各自既有顺序／CAS 合同，不套用 Goal 的串行规则。

需要写后确认时，读取携带该资源的 minimum_version（目录沿 minimum_catalog_revision 表达），取回的相同 owner／代际版本必须不小于提交水位；完整回执已满足展示需要时不额外强制重读。不能合并进无法满足该水位的旧在途读取，not-modified 也必须满足它。后续自然变化可以返回更高版本，不要求对象仍等于当时回执。最低版本针对原资源，不把 Job、队列和目录版本互相比较。

后端查询 operation 返回已知结果或明确未知／已失效；未知不证明原操作未执行。仅在 owner 能继续验证去重身份时允许原身份重试；业务对象已经删除或去重依据已退出支持范围时拒绝自动重放，保留可见诊断。去重记录复用原 owner 的准入／操作存储，不默认给所有命令设置 TTL。未确认操作、身份关联和必要去重事实不得作为普通缓存淘汰；大载荷不在回执重复保存，已确认本地临时记录及时清除。

### 5. 本地可靠提交按真实需要接入

导航复用现有 IndexedDB outbox。消息提交在第一次发出请求前，按 operation_id 在现有客户端存储中持久化一份不可变请求输入：冻结正文、目标归属、运行／投递选项和有序附件描述，不只保存可变草稿键。Composer 草稿仍可独立继续编辑／清空；恢复、原身份重试及冲突校验只消费该冻结输入。冻结操作完整提交后才允许发网络请求，持久化失败则不发送，避免只有后端可能接收而本地没有稳定身份。

已有不可变且持久的附件对象可复用引用；如果当前只有会被覆盖的草稿记录、临时 blob URL 或页面内字节，则在原存储中保留一次可恢复的输入，不凭临时 URL 承诺恢复，也不新造全局 blob 引擎。未确认记录持有必要引用，编辑下一条草稿或清理缓存不能删掉它们。后端确认接收且业务身份交接可恢复后，删除冻结输入并释放仅属于该操作的引用，不删除其他草稿／业务数据共享的附件。重新发送编辑后的正文是新用户操作，使用新身份，不修改原快照再用旧身份重试。

只有权威 owner 明确确认原操作未被接收且已终结拒绝时，本地才将它转为可编辑失败项，停止自动提交和未知结果对账。可直接复用冻结记录承载该失败项；向可编辑草稿或明确重提的新操作完整持久交接输入，或用户明确丢弃后，清理旧操作及其独占引用，交接不得覆盖后来编辑的草稿B。尚未发出请求的操作可直接取消；已经发出而结果未知时，本地取消或关闭错误提示不证明未接收，不能删除唯一对账依据。任意409／5xx不自动视为原操作终结拒绝，身份载荷冲突尤其须先用原冻结输入对账；明确失败项在重启后也不重新进入自动发送循环。

Goal 的普通设置默认同步请求，不增加持久 outbox或离线编辑；当前页面内仍以原操作身份处理不确定结果。刷新后读取权威 Goal；不承诺关闭页面后自动恢复尚未完成的编辑意图。协议允许以后为确有需要的命令选择持久提交，但不要求所有领域复制消息机制。

清理要分三类：前端未确认记录在权威交接后删除；后端 pending 项由原调度器消费；Goal／历史由原业务生命周期保留。前端 accepted 后不得继续从本地队列调度，页面重启对账也不得重新入队。后端出队后崩溃仍沿当前保证，不新增 delivery attempt恢复或“自动补发”。

队列与 Job 的关联复用原调度锁内生成的 dispatch.queue_snapshot_version：标志出队／开始执行的权威快照或事件携带该队列水位，并与相应状态转换处于同一调度提交边界，不另造跨资源序号。客户端为当前 owner／Thread 的队列保存已确认的最高水位，即使队列为空也不从列表元素重新推导；低于该水位的快照不应用。同一 Job 执行生命周期内，已确认开始／终态不能被 queued 投影覆盖。不携带状态关联的失效通知仅触发有界队列／Job确认，不拿 Job 版本或事件cursor冒充队列水位；迁齐状态投影消费者后移除旧无关联状态合并。该水位随原范围缓存释放，不新增持久队列副本。

### 6. 快照与事件有明确接缝

复用 workspace observation 的现有连接与领域事件，不为每个资源创建独立传输。初次读取和重连使用权威快照及其关联事件水位，从水位之后补读；已有领域无法提供同一提交边界水位时，先订阅并暂存通知，再读快照后确认变化，不能只先读后订阅而无补读。

流内保持原 cursor／顺序合同。重复或迟到事件按资源版本处理；游标过期、缺口与重连后不能确定同步时，合并重读受影响资源。鉴权、owner错配、载荷错误直接报告，不把它们当成可无限重试的断网。一个领域没有可比较顺序时只把事件当失效通知，不发明通用增量合并。

业务提交与事件发布可能分开成功；“仅发布已提交事件”不等于每次提交必有事件，cursor也不能证明没有遗漏未生成的事件。首批 Goal／会话执行摘要默认复用现有可见性与低频调度，在页面可见且观察有效期间，最长每30秒合并确认一次当前观察资源的权威版本；焦点恢复、重新可见与重连时立即确认。确认经过Gateway原路由，不能停在未再验证的镜像或事件日志水位；版本与公开投影对应同一权威提交，有变化才读取有界对象。失败明确标记未确认／stale及原因，不以仍收到SSE心跳表示已同步。仅确认当前观察范围，不扫描全部会话、历史或内部记账；隐藏／释放的范围停止调度，重新进入时先确认。目录沿已有定时权威版本确认，不重复增加轮询。此机制覆盖提交后通知失败且连接未断的情况，不新增全局通知outbox；通知失败仍沿原错误路径报告，不吞错假报成功。

### 7. 目录镜像独立再验证，前端只维护范围缓存

```text
前端请求目录页（owner、父节点、页范围、已有版本、可选最低确认版本）
  → 普通浏览：当前 Gateway 返回适用镜像页及来源／新鲜度
  → 写后确认：镜像达到最低版本才可用于确认，否则经原路由读取所需权威页
  → 合并同 owner 的后台版本确认，经原路由访问目标 Gateway／Backend
  → 未变化：更新确认状态，不导出全量、不重建索引、不重写相同快照
  → 有变化：原公开导出更新镜像，原子发布成功后通知前端相关目录失效
  → 前端重新读取受影响的已加载分支；移动涉及源／目标批次一致发布
```

没有镜像时有界读取所需权威页，后台镜像建立不阻塞所有目录页或其他目标；磁盘落盘失败明确报告镜像同步失败，普通权威读取仍按自身真实结果交付。缓存未变化响应必须对应同一个范围；Gateway 缓存与前端相同只证明镜像一致，是否已经远端确认是独立事实。请求触发再验证与定时同步共用原 owner 的合并机制，不每次输入触发全量同步。

确认读取把 committed_catalog_revision 作为下限，经所有 Gateway 透传到 children、breadcrumb 和所需 snapshot／范围读取。镜像103不能确认已经提交的105，也不能返回 not-modified 或用103的404判定105下的节点不存在；直接读取105或更新的权威页，无需等整份镜像导出／落盘。目标不可达、权威版本仍不足或原代际已失效时，在原请求预算内明确失败，前端保留待确认状态；禁止退回低版本镜像结束本地 operation。已达105的前端不得因下次先返103而回退；最低水位在完成对账后仍由已知资源版本防止回退，原 outbox 清理继续要求应用投影达标。

在线搜索继续查询原领域 owner，只有原允许条件使用 stale 镜像；保留搜索 cursor、错误和结果来源合同。浏览目录的镜像支持不据此改成“所有查询都离线优先”。

目录版本变更后的全量公开导出暂留后台边界，先消除无变化的导出／落盘。普通请求不等待全量导出；若实测后台全量仍影响交互，再在同 owner 明确提出有界分页导出需求，不预写通用 delta 事件库或全局 SQLite 索引。用户允许必要时 SQLite，不代表本次强制迁移已有派生 JSON 镜像。

### 8. 验证按行为与风险，逐切片删除旧实现

复用现有 Goal、导航 outbox、pending 队列与多目标测试。只补能区分已知错误的行为场景：迟到覆盖、提交成功响应丢失、重复提交与冲突、交接中刷新、事件缺口、目标切换和目录移动混合版本。额外核对写105而镜像103时不能完成确认、消息A待确认而草稿改B／清空后的恢复、同客户端设置后清空及两客户端同控制版本写入、长期记账后的公开 Goal 响应体有界。复用现有场景补证通知发布失败但SSE仍在线时第二客户端收敛，明确拒绝后的编辑／丢弃不遗留自动重试，队列清空后旧快照仍被拒绝；结果未知时的必要输入继续保留。复用少量可控网络故障入口，不绑定某条 SQL、内部调用次数或 helper 名称。

公开签名变更先生成并核类型；每个完整代码批次做相关静态分析，涉及 Web 完成真实类型检查和 `bun run --cwd src/clients/web build`。协议沿 Buf 与既有生成检查，breaking 对真实基线核对授权删除，不为通过保留旧接口。普通读取的性能证据使用 `add-test-performance-observability` 的已有工具，单次超预算保留原样本，不增加生产监控框架。

对真实提交 `cec854067a9f0238e83a2dd673dd996d3b3c9ff8` 的 Buf breaking 核对只出现本轮明确删除的四项：旧 `SessionGoalClearResultDTO`，以及公开 Goal 的 `last_accounted_job_id`、`last_continued_job_id`、`accounted_jobs`。清空改用带操作身份和控制版本的请求／回执，内部记账继续归原 owner；不保留这些公开旧结构。原始输出位于 `out/tests/temp/2026/11/13/state_sync_messages/artifacts/protocol-breaking-v1.log`，工具按预期报告 breaking，退出码100，不表述为无破坏性变更。

修改前的三条入口已在首次同步实现提交 `0afb79e3f` 的直接父提交 `ae41b8cee897bc69ed4f2369967528d43d02cac6` 采集真实基线，任务1.2完成。历史完整checkout经既有worktree工具准备，构建和采样期间保持clean；只在本任务隔离工作区和Home启动真实双Gateway、localhost SSH和同一Backend，不运行模型。临时driver复用既有Perf与页面观察器，保留原凭据读取、响应完整消费、三API各21轮以及关闭→三跳→真实点击→Composer可编辑的21次导航；新同步写入协议和故障正确性断言不适用于历史版本，没有修改历史生产代码或业务响应。首次启动因缺少原pytest autouse manifest在Backend初始化失败、没有计时样本；补齐本实例manifest后唯一真实采样退出0，失败原件保留。两边规模均为2登记工作区、1Session和1folder、空Goal／pending；before首次导航1352.3ms，暖p50／p95／max为1276.6／1317.6／1334.4ms。Goal暖p50／p95／max为64.0／69.0／69.7ms、89字节；pending为92.3／107.4／125.6ms、245字节；children为65.7／73.3／75.6ms、1342字节。历史Goal不含新增owner／版本投影，目录不含新增sync确认metadata，字节差异原样保留。当前候选45的同负载描述统计见下；已有报告器compare因测试population及稳定ID路径不同明确返回comparable=false，不将不同协议和非同期完整版本描述为严格因果对照或零回归证明。原始样本、3144份历史源码指纹、driver／bundle指纹、准确命令和运行边界见 `out/tests/temp/2026/11/13/state_sync_catalog/artifacts/perf-before-receipt-47.json`，标准比较原件为 `perf-standard-comparison-47-vs-45.json`；两边均20个暖样本、无丢记录、未筛慢样本或放宽预算。

完整浏览器正确性验收已在 `run-bc25ca8b8b09` 完成：两个独立观察者经当前及第二 Gateway 访问同一 Backend，Goal 设置／清空、同版本409和旧身份重试、真实断网恢复、目录改名／移动及实际关闭再点击重开会话均通过。消息参数只在真实 POST 提交后丢弃响应并暂时阻断原身份 GET；刷新后按原身份查询、保留草稿B，两端显示同一回复，仅一次POST且同一Job由running收敛为completed。使用正式Workbench final-v5与统一model_stream回放，因此为Integration。此前两次建图冷启动超期原件仍保留；本次在包含工具启动改动的HEAD `ae41b8cee` 上通过原期限，没有放宽等待或预热Agent。

该轮性能门未通过，后续候选49的完成证据见下。规模为2个登记工作区、1个Session和1个目录；三条API各20个暖样本的p95分别为Goal 85.2ms、pending 127.4ms、children 18.2ms，响应分别239／245／1573字节。完整关闭编辑器→工作区列表→当前工作区→目录页→真实点击会话→Composer可编辑的暖p50为1276.1ms、p95为1792.8ms、max为4067.0ms，超过既有1000／2000ms预算。保留全部慢样本，不将已有页面可操作的旧测量混作真实重开结果，不调整预算；继续沿原性能工具分解实际重开链路，未增加生产观测框架。原件位于 `out/tests/integration/clients/web/test_cross_end_state_sync/runs/run-bc25ca8b8b09/nodes/node-540cb8bddd6d/artifacts/measurements.json`；本轮pytest总计1通过、1性能断言失败，不能表述为整组通过。

性能纵向切片删除了两次已由同一权威响应覆盖的串行读取：会话树直接消费 Session DTO 中由 catalog 冻结的主 Thread 身份，聊天初始化保留 Session／workspace 校验并删除前置 `/workspace`。其余 `mainThread`／`assertWorkspace` 真实调用方保持原合同；迟到导航、未读、附件初始化和元数据页面的必要消费者已迁齐。原最慢 `/workspace` 的响应头约16.8ms，浏览器 requestfinished 约2014.8ms，同 request_id 的两层 Gateway 与 Backend 日志已返回200，不能把该等待全部归给目标服务；原件与归因边界见 `out/tests/temp/2026/11/13/state_sync_catalog/artifacts/perf-original-slowest-28.json`。固定 VS Code `fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca` 在 EditorPane 完成 setInput 后发布活动编辑器事件，本切片保持其生命周期及原请求失效序号，没有新增缓存、交接状态或调度框架。

历史定向复验 `run-afa9a498fd9b` 的业务及浏览器进程退出0，pytest因导航预算退出1；后续候选49的完成证据见下。实际正式候选 bundle 的 main SHA 为 `8b8a401d7a5236181e01d376f125d2654f6f52667cd7116931ba0030a4f89087`，两处性能 TSX 保持 `dda2695d…`／`e9c98e31…`；QuickAccess 后续有独立源码变更，本证据仅覆盖该候选的状态同步入口。原21次导航、20暖样本、三跳顺序、完整可编辑终点及1000／2000ms预算均未改变：首次1479.2ms，暖p50 1345.4ms、p95 1673.1ms、max 2484.5ms。Goal／pending／children暖p95为89.9／116.1／23.1ms，响应239／245／1573字节。新增的既有 Perf 子阶段显示，关闭暖p95 56.2ms，工作区列表521.4ms，当前工作区87.3ms，目录49.8ms，点击61.9ms，点击完成后等待Composer可编辑1026.4ms；阶段分位数不能相加推导总体分位数。最慢样本的工作区列表631.1ms、等待可编辑1466.5ms；点击后的请求窗口仍有3次工作区登记和3次Session读取，后续bootstrap→history及资源初始化、订阅确认也在该等待内。继续优化前应精确核原活动编辑器回调与Chat初始化的重复读取和可编辑终点的依赖，不能凭时间窗口将另一个观察者的后台请求当作确定调用方。全部样本与原始阶段记录见 `out/tests/integration/clients/web/test_cross_end_state_sync/runs/run-afa9a498fd9b/nodes/node-540cb8bddd6d/artifacts/measurements.json` 和 `out/tests/temp/2026/11/13/state_sync_catalog/artifacts/perf-phase-analysis-31.json`，未丢记录、未筛慢样本，也未无证据重复长跑；当时缺少的任务1.2实现前基线已由上方历史checkout采样补齐。

定向候选 `run-7432bdd1f95c` 沿原21次导航、20暖样本、三跳顺序与Composer可编辑终点完成浏览器业务验收（退出0）；pytest仅p95性能断言退出1，任务6.2仍未完成。正式bundle main SHA为 `9801de562a73b0f699df73cbcb42aac4d2f436a3a5a70f97db11facf8f80442c`，构建窗口内导航／Chat初始化与历史hook源码未变；并发工具与配置面板源码变化单独记在 `perf-run-source-39.json`，不据此声明这些独立入口已验收。沿所属Pane消费已解析owner，删除活动导航的第二次路由解析；历史bootstrap与原宿主seed并行，只把同次Promise交给首次历史装载，刷新及partial轮询继续真实读取后端，不增加缓存。31个历史定向用例通过。首次导航1448.3ms，暖p50 874.7ms、p95 1350.4ms、max1512.4ms；Goal／pending／children暖p95为86.2／107.9／21.5ms，响应仍为239／245／1573字节。工作区列表暖p95 203.3ms（同候选包含健康读取错误修复），等待可编辑暖p95 1003.8ms；阶段分位数不能相加，不能将全部改善归于前端改动。gateway-a真实access按request_id确认第一页面从3次路由登记降至2次，仍有3次Session读取；bootstrap已与宿主读取并行。原件为 `out/tests/integration/clients/web/test_cross_end_state_sync/runs/run-7432bdd1f95c/nodes/node-540cb8bddd6d/artifacts/measurements.json`，阶段与请求归属收据为 `out/tests/temp/2026/11/13/state_sync_catalog/artifacts/perf-phase-analysis-39.json` 和 `perf-navigation-request-timeline-39.json`，无丢失样本。

同一候选 `run-a37df5ea92ed` 通过原共享构建装具运行导航性能、`basic_chat_tool_loop` 和 `browser_view_sessions`：构建与Web／Workbench真实类型检查均退出0，历史定向32项通过；三项产品结果为2通过、1性能断言失败，不能表述为整组通过。正式bundle main SHA为 `f9c88d2d37da6ce2746f8fb495535046f55bffd7a9ae47ad084dd0a85bfbb270`，构建和运行窗口产品源码未变。目录点击的同次权威Session经原editor options传至所属Pane及首次Chat seed；恢复与重试仍真实读取，不序列化Session或新建registry。宿主只接管挂载前的Promise拒绝观察，原历史owner继续显示失败、保留只读缓存及显式重试；新增用例覆盖首读先于挂载失败。21次导航均按request_id与第一页面Gateway access确认Session读取从3次降至2次、路由登记2次、bootstrap首读1次。首次导航868.1ms，暖p50 775.6ms、p95 1215.6ms、max1242.6ms；Goal／pending／children暖p95为80.1／96.0／17.1ms，响应239／245／1573字节。工作区列表暖p95 191.9ms、等待可编辑894.6ms，保留全部慢样本、三跳顺序和原1000／2000ms预算；任务6.2继续未完成。真实浏览器另证迟到目录读取不夺回焦点、原版预览／固定／关闭重开与真实消息工具循环通过；延迟精确Thread bootstrap时，已有缓存正文含24个真实禁用控件、Composer不出现，权威历史返回后恢复操作，独立浏览器无旧正文，未读归属正确。完整原件、源码指纹和边界见 `out/tests/temp/2026/11/13/state_sync_catalog/artifacts/perf-navigation-receipt-45.json`；正式性能原件为 `out/tests/integration/clients/web/test_cross_end_state_sync/runs/run-a37df5ea92ed/nodes/node-540cb8bddd6d/artifacts/measurements.json`。后续只依据明确的新故障或可行动阶段证据继续优化，不以无变化重跑碰预算。

最终候选49在原 `ThreadInput.resolveOwner` 的真实目录读取中保留同次返回的工作区列表，沿所属Pane直接交首次Chat初始化；删除这次打开中已被同一权威响应覆盖的第二次目录请求。原唯一owner校验、凭据／Gateway失效、在途Promise合并、取消与错误显示保持，重开及显式重试仍读取真实目录，不引入缓存、registry、序列化字段或持久化副本。迁齐消费者及43项定向用例（591个断言）通过，最小实现复审确认无需新增非业务机制；真实Workbench类型检查 `management-typecheck-11.json` 和Web构建 `management-build-08.json` 均退出0。固定main SHA为 `0adfe073a31a3b1dd7fd1054b75b1a38ec3ffe9f4466e23e46d0217f354cd7d6`，构建指纹与性能运行源码逐项完全匹配，前后无变化。

正式完整入口 `run-b252635abd85` 通过（pytest 1 passed），任务6.2完成。原21次导航、20暖样本、三API串行顺序、关闭后真实点击和Composer可编辑终点、1000／2000ms预算均保持。首次1339.1ms单列，暖p50／p95／max为713.0／768.2／785.6ms；Goal为68.8／76.2／76.7ms、239字节，pending为87.6／116.5／120.7ms、245字节，children为11.5／16.3／17.0ms、1573字节。规模仍为2登记工作区、1Session和1folder、空Goal／pending，不代表深历史或大型目录。按request_id命中第一页面gateway-a的真实access，21轮点击至可编辑窗口均为目录读取1次、Session读取2次、bootstrap1次，确认相对候选45的目录2→1；双观察者背景请求未混入归因。三跳暖p95分别189.9／78.5／25.0ms，点击后等待可编辑420.4ms；阶段分位数不相加，未将非同期候选全部改善归于这一处改动。原始Perf无丢记录，未筛慢样本；收据、各跳与请求归属原件为 `out/tests/temp/2026/11/13/state_sync_catalog/artifacts/perf-navigation-receipt-50.json`、`perf-phase-analysis-50.json`、`perf-navigation-request-timeline-50.json`，正式完整样本位于 `out/tests/integration/clients/web/test_cross_end_state_sync/runs/run-b252635abd85/nodes/node-540cb8bddd6d/artifacts/measurements.json`。上述失败候选按原件保留；本轮证据不替代未重跑的无关页面、平台或全仓验收。

迟到导航定向回归 `run-80348b99ab6b` 已通过迁后的真实 Session GET、精确 owner／Thread／request_id、迟到不抢焦点、预览固定及关后重开；整例随后在最终回复阶段因后端 `graph_binding_unavailable` 失败（registry 中没有 `deep-agent` factory），原始退出码1。错误发生在后端先解析 binding、后续 runtime builder 才懒导入 factory 的既有链路；本切片未改该后端，也未增加预热或延长等待。后续双Thread／文件断言未执行，未读、附件及元数据三个完整页面入口仅完成迁移与静态检查，未表述为已重跑。原始 stderr、真实 stream.failed 和边界分析保留在 `out/tests/temp/2026/11/13/state_sync_catalog/artifacts/perf-navigation-contract-analysis-32.json`。

补充收口验证：消息队列通过产品入口在 claimed／started 两个阶段硬退出并重启，2项均通过，保留既有出队后恢复合同，未自动重放新的执行；原件在 `out/tests/integration/backend/sessions/test_execution_queue_product_recovery/runs/run-2032fbf629bd/`。启动摘要本身不含队列水位，零活动计数也读取唯一权威 pending 快照，收到有效空队列版本后才清旧项；相关历史入口35项通过。Goal 旧执行失败／取消与用户暂停／编辑／替换并发时，原 owner 在同锁内检查Goal身份、活动状态和观察控制版本，过时工作正常结束；未知错误仍暴露，相关业务与运行器49项通过。最终前端类型检查与Workbench final-v7构建通过，完整浏览器原件仍对应前述final-v5，未将后续窄修复伪称重跑整组；检查记录为 `out/tests/temp/2026/11/13/state_sync_messages/artifacts/final-checks-v7.json`。

真实验收经 Workbench → 当前 Gateway → 本地／第二 Gateway → 目标 Backend，使用隔离工作区和独立实例，不启停其他开发服务。覆盖目标初始化、精确归属、响应 request_id和真实 DOM 行为；两个观察客户端验证最终一致。暖读至少20次，计完整响应；目录导航与会话打开计真正确认及可操作终点。回环无外部依赖使用既有预算，SSH／外部网络等待单列但不删除慢样本。

## Risks / Trade-offs

- [误把不同版本语义统一] → Goal 原 revision 保持记数语义，新控制 CAS 与公开同步版本各有明确作用；队列 snapshot_version、catalog_revision 和事件 cursor 继续归原 owner，写后最低版本只比较对应资源。
- [冲突处理变成自动覆盖或重试循环] → 同客户端串行，多客户端原子 CAS；409保留输入并等待明确重提，纯记账变化不使控制写入持续冲突。
- [冻结输入增加本地存储] → 仅保留未确认消息，复用实际持久附件引用并在权威交接后释放，不重复持久化已接收历史。
- [可靠接收变成新的调度／恢复系统] → 只保证同身份不重复接收，保留既有出队后契约；异步执行状态归原 Job／导航 owner。
- [抽象增加更多状态与文件] → 先迁两个真实用例提取公共原语，再接目录；复杂度复审必须指出每项新增机制对应的具体故障，并删除被覆盖旧逻辑。
- [镜像提升速度却掩盖过期或故障] → 来源、同步时间、确认和错误分开；stale 预览不算完整验收，权限失效撤销展示。
- [已有规格与并发工作不一致] → 按当前用户目录规范和 owner 实现核对；本次不改写他人变更，实施前解决导航历史规格归并，归档时只合并本次增量。

## Migration Plan

1. 确认当前调用方、协议生成入口及相邻规划边界；固定三条切片的行为与性能基线。
2. Goal／会话状态先迁齐端到端合同与客户端默认读取；再迁消息接收、队列交接；最后接目录镜像及原导航 outbox。每条切片完成静态分析与定向验证后清除被替代链路。
3. 同一领域的声明、生产者、间接调用方及测试整体切换，不部署新旧协议混合版本。回退使用匹配的一整套源码／构建与隔离开发数据，不在生产路径保留 fallback。
4. 不为未发布开发数据增加旧格式迁移、扫描重建或静默修复；结构不匹配明确报告，使用本任务可丢弃的隔离数据验证。若后续用户指定需要保护的真实数据，再按对应 owner 单独设计迁移。
