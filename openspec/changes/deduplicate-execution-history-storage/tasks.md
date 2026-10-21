## 1. P0 依赖与恢复合同（先于结构改动）

- [x] 1.1 固定当前锁定/安装版本、官方发布及源码指纹；核对模型节点、DeltaChannel、消息reducer、serializer、new_versions、writes_sort_key和同步/异步增量历史合同，并登记全部实际产品调用方。
- [ ] 1.2 用相同真实产品工作负载保存优化前的各表/正文/索引增量、serializer调用/字节、写入/恢复时间，区分既有canonical消息复用与仍重复的channel、assembly、view成本。
- [x] 1.3 将低于目标的依赖更新至LangGraph1.2.16/checkpoint4.3.0或当前更高版本，不回退；迁齐模型节点适配，验证真实图同步/异步执行、消息合并与checkpoint冷恢复，独立提交，不宣称去重完成。

## 2. P0 非 messages 通道版本复用

- [ ] 2.1 冻结本片schema变更和明确支持的旧库范围；实现owner/namespace/channel/version不可变正文及checkpoint引用SQL、有序升级/备份/失败恢复，检测旧同版本异内容而不强行合并。
- [ ] 2.2 将new_versions从put/aput贯穿唯一writer/持久层；在变更边界冻结/序列化新版本，区分明确沿用引用与同版本新候选，拒绝内容碰撞及原地修改冒充未变化。
- [ ] 2.3 迁齐读取、列表、历史checkpoint、pending state和同步/异步；保持None/absent、updated顺序、serializer/hash及独立解码对象，不隐式借用parent/latest值。
- [ ] 2.4 迁齐fork捕获/安装/恢复闭包和历史scope引用；验证同version异Thread/namespace隔离、冷重启、故障原子性，并删除逐checkpoint正文旧读写实现。
- [ ] 2.5 以产品执行和定向碰撞/迁移验证证明未变化大channel不重复序列化/写正文，保留前后指标、静态与独立审查，提交并push。

## 3. P1 官方消息增量与基准恢复

- [ ] 3.1 基于1.2测量确认现有messages正文复用之外的增量收益，核实产品create_agent最终channel声明及DeepAgents reducer差异；冻结官方snapshot计数/频率与serializer合同。
- [ ] 3.2 迁齐图状态、Saver写入、canonical引用delta、基准snapshot和metadata计数；保留新增/替换/删除/Overwrite/摘要语义，不另存完整消息历史。
- [ ] 3.3 完成同步/异步get_delta_channel_history与有序重放，沿父链按task_path/task_id/idx恢复，排除目标pending writes，明确区分合法空根和损坏断链。
- [ ] 3.4 迁齐fork/rewind/compaction及进程重启；目标本地带齐基准和全部必要中间writes，验证深历史、A→B→C、源删除后恢复与工具配对，移除旧路径。
- [ ] 3.5 通过真实产品入口和同步/异步图验收比较恢复结果、存储/序列化收益，完成静态/审查后独立提交并push。

## 4. P1 Assembly 与视图结构共享

- [ ] 4.1 测量assembly完整请求、prefix frames、工具/资源正文与view成员重复，确定有序typed引用、共享prefix/view节点的最小结构和实际schema升级范围。
- [ ] 4.2 迁齐assembly seal、唯一writer和冷reader，复用canonical/工具/资源不可变版本与前缀，保留选择/省略/贡献/protection并只凭冻结内容重建normalized request和request_hash。
- [ ] 4.3 迁齐view写读、历史分页、checkpoint selection、rewind/compaction与fork，按共享基准/成员变化保留visible/context_selected与logical/context ordinal，删除完整成员重复写入链。
- [ ] 4.4 验证资源变化、故障发布、深fork、冷恢复和历史hash；相同负载证明多个assembly共享内容且索引增长减少，静态/独审后提交并push。

## 5. P2 引用保护回收

- [ ] 5.1 由retained checkpoint、assembly、resource snapshot、fork/claim与既有retention根构造状态版本、prefix/view、增量基准和中间writes依赖闭包，避免第二套可变权威状态。
- [ ] 5.2 先回收无引用冗余；需要换基准时在原事务内建立/验证等价基准并切换引用再删除，保留用户原有历史期限及可回退/fork点。
- [ ] 5.3 用产品生成的历史在清理前后执行重启、恢复、深fork、回退和继续，验证身份、顺序、工具配对、owner与request_hash不变，失败中断不破坏依赖；移除旧清理路径并提交。

## 6. 最终验收与证据

- [ ] 6.1 在相同版本标记/工作负载下通过Gateway和真实目标workspace执行多轮工具循环、并发工具结果、摘要、进程重启、深历史fork和回退；保留同步/异步及真实HTTP/SDK专项证据，普通业务/Web复用model_stream与共享协议资产。
- [ ] 6.2 对照优化前后各表体积、正文/索引增长、序列化次数/字节、写入/恢复时间，分别证明channel版本复用、assembly共享、回收后恢复/fork，不以总库体积或升级成功代替证明。
- [ ] 6.3 复核无第二事实源/双读双写/过时接口、无历史版本改写；相关静态、类型/构建（若涉及Web）、严格OpenSpec及独审通过，按片提交并push。无法短期解决的性能长尾以测量证据和原目标登记TODO。

优先级裁定：本变更1/2组影响后续全部执行恢复存储，优先于已完成fork核心的纯性能长尾；3/4组依赖它们的合同，但不能借Delta接入遗漏assembly/view去重；5组最后，避免清理早于引用关系。现有正确性缺陷仍先处理。`extract-sqlite-schema-assets`只做独立纯重构，已有验证批次继续收口，不列为本变更前置依赖。

2026-10-21 依赖合同片：LangGraph1.2.16/checkpoint4.3.0、SDK0.4.8已安装并锁定；源码和全部直接消费者指纹见 `out/tests/temp/openspec_takeover/artifacts/history-dedup-contract-audit-001.json`。模型节点同步/异步2例、官方Delta图及Saver恢复合同12例、升级后的Gateway四轮工具循环/并发结果/重启/历史request_hash均通过。集成矩阵89例通过，另外2条锁mock原本误拦截fcntl而实现使用portalocker，恢复正确mock后与真实HTTP/SDK同步异步专项一起通过，原失败保留。独审无升级阻断。升级不等于去重完成。

1.2仍待补齐摘要、深fork与回退同负载；已保存优化前和升级后的四轮工具/重启工作负载，分表正文/索引字节、serializer计数及耗时。旧依赖基线冻结在 `out/tests/temp/openspec_takeover/artifacts/history-dedup-baseline-frozen-001/`；正式原始测量在 `out/tests/integration/backend/sessions/rollout_context/test_execution_history_storage/artifacts/run-*/measurements.json`。不得以这些局部场景冒称全部验收。
