## Context

动机与范围见 proposal。2026-10-21 核实：本机与 uv.lock 的 LangGraph 为1.2.0、checkpoint为4.1.0；PyPI目标1.2.16发布于2026-10-16，checkpoint4.3.0发布于2026-10-12。元数据、sdist SHA256与从发布包读取的官方源码保存在 `out/tests/temp/openspec_takeover/artifacts/history-dedup-upstream-001/`；不把安装成功当功能验收。

现有 `checkpoint/runtime/langgraph_api.py::put` 消费new_versions补全版本，却没有将它传给writer/持久层；`storage/transaction_projections.py::_insert_checkpoint_channels`对每个非messages值重新_encode并存BLOB。messages已使用canonical正文和冻结view，不应重复复制正文来接入新机制。产品图由 `app/agents/model_call_graph.py` 调用create_agent构造；产品messages仍是AgentState/add_messages；实际FilesystemMiddleware的files已使用DeltaChannel（snapshot_frequency=50）。不能将DeepAgents默认图的messages声明当作本产品声明。

官方目标版DeltaChannel为beta：非snapshot步不在channel_values保存该channel，基准使用 `_DeltaSnapshot`；`get_delta_channel_history`沿parent_config遍历，而不是按时间list，排除目标checkpoint自身待应用writes，逐步按 `writes_sort_key(task_path,task_id,idx)` 还原。快照由更新计数和superstep上界共同触发，metadata中的计数必须保留。plain value可作旧聚合channel的基准。上述合同来自已校验sdist中的channels/delta.py及checkpoint/base、serde、memory源码；具体实现只使用本项目现有owner。

目标版官方 `_messages_delta_reducer` 不处理REMOVE_ALL_MESSAGES、未知ID删除报错及chunk转完整消息；其 `ensure_message_ids` 仅对精确官方reducer补齐所有message-like输入。消息片必须迁齐清空/摘要调用方并验证这些边界，不能单改Annotated或复制另一份reducer。当前已修复pending writes同task_path时缺task_id排序的问题，同步/异步冷读取和父链恢复均有回归。

## Goals / Non-Goals

**Goals:** 按版本共享不可变状态、按基准和增量恢复消息、按不可变内容共享assembly及前缀/view结构，并保持执行/fork/回退的身份、原子性和历史hash。新实现的重复正文与索引增长必须可测。

**Non-Goals:** 不更换SQLite、不引入另一个官方saver，不为所有小字段实现通用JSON diff，不改变历史保留期限、provider请求参数、canonical身份算法或历史事实。SQL抽离不是前置条件。

## Decisions

1. **按影响排序。** 先完成依赖与同步/异步合同闭包，再推进非messages复杂channel版本复用；消息Delta接入同时覆盖状态定义、saver、写读、fork与恢复；随后assembly/view共享，最后引用保护回收。现有正确性缺陷优先；已进入验证的SQL纯提取可独立收口，不阻塞本变更。性能量化和长尾优化在结构正确之后，原性能门不放宽。
2. **保留唯一写入链。** `put/aput → RolloutAppendWriter → checkpoint owner → 既有SQLite事务`统一携带new_versions和确切owner。状态值以 `(source_session_id, source_thread_id, checkpoint_ns, channel_name, channel_version)` 定位，记录serializer、长度、内容hash与不可变正文；checkpoint行保留完整逻辑快照的引用、updated顺序和值状态。历史来源owner与当前物理存储owner分开，使用现有冻结scope binding解析，禁止相同版本跨Thread混用。
3. **变更边界序列化一次。** 新版本在唯一写入边界序列化/冻结并保存内容标识；未变化版本以明确的版本引用复用，不因checkpoint数增长重复序列化复杂值。重复声明既有版本时核对冻结内容，同键不同内容拒绝。实施第一片必须明确区分沿用既有引用和为同版本提供新候选值的入口，验证可变对象被原地修改的冲突，不能通过无条件忽略输入换取少序列化；不确定来源不得静默借用parent/latest值。None、absent、delta无新snapshot、空messages四种状态必须分清。
4. **复用上游增量协议。** 使用官方DeltaChannel和消息reducer的新增/同ID替换/RemoveMessage/Overwrite语义；不自写一套近似reducer。使用官方serializer的snapshot形态与版本计数，增量记录只引用canonical item/冻结版本及顺序坐标，不复制完整消息历史。Saver的同步/异步历史接口返回同一基准和有序writes。已知应有基准或祖先缺失、hash冲突和跨owner引用必须报错，不能把断链解释为合法空起点。
5. **对现有复用测增益。** 接入前先统计canonical正文已省去的复制，分开计算剩余serializer调用、channel BLOB、pending writes和view成员开销。官方Delta不能自动优化assembly或索引；其收益独立报告。快照频率遵循所选官方版本合同，不未经测量写死新的全局调参配置。
6. **assembly保存可重建的封存引用。** 复用canonical payload、工具schema版本、资源版本/受保护detail和不可变prefix节点；assembly保存有序引用、选择/省略、贡献坐标、必要delta和封存metadata。normalized request必须可只读历史引用重建，并按现有规范化/hash算法验证request_hash；冷读不访问当前文件/配置/工具注册状态。view成员以共享基准加成员增删/范围表达，查询保持logical/context ordinal、visible与context_selected语义，并界定合法重建深度；避免每次复制完整成员表或把每份大JSON外移成文件。
7. **原事务与发布边界。** 继续先durable canonical bytes，再在原有SQLite事务发布checkpoint引用及相关业务状态；metadata-only与item-bearing边界不变。新blob/引用/assembly/model call/resource binding不得以多个可观察的分步状态发布。注入失败后无悬空引用、孤儿可见checkpoint或伪完成；冲突沿现有详细错误链暴露。
8. **fork复制闭包而不改写来源。** 复用原snapshot捕获/安装和typed引用决策；在目标可用前带齐每个delta channel的基准、on-path中间writes、共享状态与assembly/view内容。保留历史来源身份和version；目标本地持有完整闭包并沿既有授权关系访问，正常冷读/继续不回源。A→B→C、父删除和同version异Thread必须覆盖。
9. **回收按引用闭包。** retained checkpoints、assembly、resource snapshots、fork claims与既有retention关系是根；向下保护状态版本、prefix/view节点、delta基准及全部必要中间writes。先清无引用内容；若需重设基准，在事务内重建并验证等价后切换引用再回收，不修改历史canonical业务事实或request_hash。不采用只留latest规则；历史期限变更另立产品合同。
10. **迁移范围显式。** 只有实际结构变化才提高schema号；实施前冻结当前受支持版本和最近旧版本的升级范围、备份/恢复与失败状态。将旧checkpoint独占值转成版本引用时逐owner验证并删旧列/旧读写，无双读/双写。已存在同owner/channel/version异内容的旧行不能强行合并，必须具体诊断并按显式可恢复策略处理；不扫描目录猜历史。不为SQL搬迁加版本。

## Risks / Trade-offs

- Delta接口处于beta → 依赖目标与源码指纹可复查；真实图执行、序列化、同步/异步、父链重放及重启恢复作为版本门，升级时重验。
- 引用只减少磁盘写入却仍全量序列化 → 同负载统计serializer次数/字节和每表增长，不以最终库文件总大小单独判断收益。
- 摘要/替换/并发工具改变增量折叠次序 → 官方reducer、writes_sort_key和overwrite边界联合验收，禁止按task_id单独排序。
- assembly共享丢失历史输入或保护级别 → 保留有序typed引用、内容hash和protection，冷恢复重新计算request_hash，保留精确错误。
- 回收或深fork断开增量基准 → 引用闭包和事务重设基准；回收后再次冷启、fork与回退，不只测试GC计数。
- 性能长尾难短期解决 → 保留工作负载、指纹、表/正文/索引增量和耗时分布的TODO；正确性与结构性去重仍须完成。

## Migration Plan

每片先固定现有产品工作负载与原始结果，迁齐声明、唯一writer、全部reader、fork/恢复与测试后集中验证并独立提交。依赖升级片不提升schema；状态版本、消息delta、assembly/view共享如改变结构分别按有序迁移登记，不预先占用schema号。切换前创建原有备份，失败维持既有recovery_required/恢复边界；不支持用旧程序直接打开新schema，应成套回退应用与备份。最后迁齐回收并移除原重复实现。

非messages版本片采用schema20：`checkpoint_state_values`以原owner/namespace/channel/version唯一保存正文；`checkpoint_channels`只保存`value_id + value_session_id + value_thread_id`的明确引用。读取同时核对这三个字段、namespace/channel/version和历史scope授权，不仅判断引用指向任一获准owner。沿父checkpoint复用时保留原正文owner，fork复制引用不重新归属；每次恢复独立解码。

显式支持19→20，以及既有18→19后再19→20。18→19登记SQL的原字节与checksum不改。19→20在备份后、同一升级事务内先核对旧正文hash/长度/owner/namespace与版本碰撞，再转为不可变正文和引用，删除旧表；冲突回滚并恢复备份，旧库标为recovery_required，原数据保留。升级协调入口不返回可被正常reader误用的中间版本snapshot，完成全链升级后显式validate_index；正常reader仅接受20，不双读旧表。

未变化版本通过new_versions和确切父引用跳过dumps，但仍与冻结值作严格结构比较，拒绝可变对象原地修改。普通内置值、容器和无自定义serializer的Pydantic模型可直接比较；扩展serializer类型继续作字节校验并留TODO。非messages的new_versions声明与快照版本不一致直接拒绝。messages旧声明入口在后续Delta切片迁齐并消除例外。

性能TODO（结构正确性门不放宽）：同负载schema19→20的`summary_restart_continue`阶段，非messages正文364696→51297字节，字典序列化23次/192606字节→12次/11348字节；该进程阶段解码128次/263659字节→181次/444959字节。整体工作负载218.21→220.26秒，单次样本不足以认定耗时改善。沿用版本的冻结解码/比较仍有成本；先保留碰撞检测，待其余结构去重完成后再优化，不能改为无条件信任对象地址或忽略候选。原始测量、前后对照、SQLite一致备份及wire冻结在`out/tests/temp/openspec_takeover/artifacts/history-dedup-state-frozen-001/`与对应baseline目录，计数只比较同一进程阶段，跨重启不累加。

验收主场景通过真实Gateway/目标workspace/产品API生成关键状态；普通业务和Web模型统一model_stream，真实HTTP/SDK/同步transport专项保留并共用协议资产。手工数据仅用于碰撞、损坏和迁移边界局部验证，不能代替产品生成的执行、fork和重启状态。

消息增量前置片保留pending row的两种生命周期状态：未发布时官方serde完整暂存，canonical正文发布后在原事务替换为有序typed引用及框架封装差异；不存在同时维护两份已发布正文的reader/writer。引用以item/group的不可变身份定位，冷读验证owner、namespace、group hash及原写入fingerprint，不按logical message ID猜最新版本。fork从冻结可保留checkpoint集合展开这些引用，再复用同一typed依赖闭包；新增backing不能扩大checkpoint集合。完整产品证据及逐owner指标见tasks。

消息版本定位以精确canonical item或冻结view selection为准，logical message ID仅用于核对身份。request仍严格比较原group内容和归属；幂等projection绑定JSONL坐标，final pointer绑定final item，rewind绑定source view，compaction cutoff使用已验证batch位置。缺失正文或多版本歧义直接失败。该纯定位重构不改变schema20，也不提前启用同ID版本写入；后续必须迁齐版本producer、view替换和fork闭包后才能开放。

TODO（带测量的长历史生成长尾）：`history-dedup-state-fixture-timed-002`在600秒外部保护下exit124，生成尚未完成，最后到job-0071、326次normal_turn。inclusive阶段时间normal_turn593.06秒、provider.prepare_total416.18秒、prepare_context337.01秒、saver_put104.58秒，不能相加或当独立耗时。原件`fixture-stage-timing-74ad28bdb22248919398b5fb7930d6ae.jsonl`及`history-dedup-state-fixture-timeout-001.json`保留。先完成结构性去重和恢复合同；该装具未通过，后续人工优化时继续原规模，不缩减数据刷绿。

消息版本前置片实际改变messages逻辑ID索引，采用schema21：messages不再以logical ID唯一，JSONL坐标与canonical anchor仍唯一定位正文。新版本必须携带唯一精确的replaces/canonical_item来源；写入和冷读同时核对历史owner、Turn、source ModelCall/assembly、logical ID、严格递增序号及独立新group。accepted root不可替换，第三方group anchor冲突明确失败；selection先排除旧正文整组，再进行stream/checkpoint carrier去重。逐成员扩展/provenance保持各自合同，不强行相等。显式20→21迁移在原有备份和事务边界内重建messages索引，保留全部旧行/JSONL坐标；18、19仍顺序升级至21，正常reader只读当前版本。

官方messages Delta切片不改变schema21：checkpoint_channels.value_state使用view（旧完整基准）、snapshot和delta；snapshot正文依旧由冻结view/canonical重建，Delta省略正文并通过官方get_delta_channel_history恢复。精确父链按namespace核验，历史writes按task_path/task_id/idx排序，排除目标pending；缺少已引用祖先或应有基准直接失败。MessagesDeltaChannel保留官方exact reducer identity及默认频率，控制输入由官方add_messages处理清空/删除/同ID重加，chunk转换为完整消息。fresh条件边在put_writes前仅给原BaseMessage规范ID；raw dict/tuple预览保持None，防止暴露持久化时无法复用的临时UUID，正式写入由官方ensure_message_ids规范。

fork保留各Delta channel的on-path祖先与必要pending、canonical revisions；synthetic messages完整基准重置其计数，其他channel使用seed父checkpoint并保留计数，避免提前应用seed pending。恢复backing属于物理历史闭包，不属于可见会话membership；view完整Turn和block继承都只读冻结parent可见成员，不能全局收集同Turn的其他版本。原产品失败现场及最终测量见tasks。Delta接入后pending/assembly正文未再下降，后续第4组必须独立证明共享收益。
