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

验收主场景通过真实Gateway/目标workspace/产品API生成关键状态；普通业务和Web模型统一model_stream，真实HTTP/SDK/同步transport专项保留并共用协议资产。手工数据仅用于碰撞、损坏和迁移边界局部验证，不能代替产品生成的执行、fork和重启状态。
