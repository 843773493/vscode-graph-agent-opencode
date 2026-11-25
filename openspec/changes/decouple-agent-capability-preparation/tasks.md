## 1. 固定查询入口与改动边界

- [x] 1.1 按 design 查询入口表核对 HTTP、Workbench、模型工具与内部 reader，逐链确认范围、当前／冻结视图、权威来源及既有派生／分页；将实际缺口按三类迁移判定写回本 design。检索待删除目录 mode、执行服务目录入口、custom 身份字段和模型准备的全部直接／间接调用方及测试，核对 GraphBinding 与共享写入方，不开展全业务重构。
- [x] 1.2 用代表性现有装具记录工具目录／详情、正式请求工具面、压缩规划和本地失败基线；对每项拟改动确认其在 LiteLLM 消除导入耗时后仍有必要，保留已有查询核心、上下文分页器、WorkspaceSearch Provider 与 Gateway 调度，不新增查询基类或总结文档。

## 2. 工具目录纵向迁移

- [x] 2.1 查询与执行共用原参数模型、公开 schema 导出和实际共享的构造代码；compact／skill_load 等复用唯一参数声明，动态 factory 保留必要本地构造。直接复用 build_deep_agent_tool_middleware、build_custom_tools 和公开 exporter；原 RegisteredToolSetProducer 的普通工具 parameters 同源化，保留 Provider 包装／排序／结构化输出。不引入 Prepared 对象、待绑定队列或通用 registry。
- [x] 2.2 CustomToolFactoryContext 的真实 SessionThread 收敛到原 invocation binding，迁齐 browser、debugging、connections、web、session_history 等消费者；定义读取保持无 Thread，删除重复 session_id 与合成身份。
- [x] 2.3 AgentToolCatalog 直接消费最小定义依赖，迁入原目录缓存；原 list／get 与内部消费者共用该读取链，容器接入原实例，不另建查询服务。迁齐容器、API 和内部调用方，删除执行服务目录方法／缓存及 custom 二次补写，保持完整 schema、禁用／隐藏项和稳定 ID，custom 原 origin／分组由既有 helper 提供，配置描述覆盖保持；验证 origin／group 策略匹配不因映射默认为 builtin 而改变；聚合时在转名称字典前拒绝重名，不让投影覆盖掩盖冲突。
- [x] 2.4 在已有共用 publication_lock 内捕获匹配的 active 配置与一次 MCP publication 引用，不借外层旧 pinned 配置拼当前目录；锁外完成构造／导出，selection 沿原事务捕获，不新增全局锁。按原 revision 失效，有界缓存仅保存可序列化定义，连接状态从原 owner 投影，删除暖读求键的全量 schema 导出，按 design 的检查对象／触发边界表复用 schema 成功回执，保留每次参数、准入、连接代次和历史身份检查，不增加缓存层、校验服务或版本账本。
- [x] 2.5 真实工具 API 定向覆盖中间件、custom、debugging、MCP 的完整参数，以及 custom 描述／分组／策略、禁用、冲突、无效返回／schema 和模型凭据不可用；用原发布点的受控交错验证配置与目录只返回匹配组合且构造不持发布锁；实际取得临时资源者按原 scope 清理，验证失败／取消不泄漏，不给无资源 factory 增设生命周期接口。

## 3. 执行顺序与旧模式删除

- [x] 3.1 在原 generation builder 中将模型无关的工具构造／本地校验前移，动态工具一次构造后直接用于执行；依赖模型／CSM 的工具留在原必要位置，复用参数声明，不以返回待绑定对象实现多阶段框架。
- [x] 3.2 保持原 admission、冻结 selector、配置／凭据 revision、generation key、single-flight、scope、publication 与封存顺序；提前取得资源时，模型／图失败及取消沿原清理机制收尾，覆盖真实新增的释放窗口。
- [x] 3.3 迁齐 runtime／factory／graph wrapper 和测试后，删除 AgentAssemblyMode、assembly_mode 参数／分支、联合返回类型、inspection Session／Thread 与假执行回调；保留原无 Graph 的受信 untrack 路径和必要校验。
- [x] 3.4 用新进程在实际 Provider SDK／模型构造、网络与图编译边界验证无效动态 factory 的真实发送入口独立性，允许必要框架类型导入，不笼统封禁整个 langchain_core／langgraph 包；验证正常执行、两 Thread 隔离、同代复用及 primary 失败后 fallback 成功，不要求模型相关绑定错误提前至模型构造前。

## 4. 收窄实际未使用的模型准备

- [x] 4.1 压缩检查直接使用现有权威配置解析与 build_model_from_provider，只解析所需模型凭据，去掉未用候选／路由／指令；保留 profile、原计数／切分和 idle／write admission，沿原 scope／调用方所有权关闭所建模型，不重复实现已存在的候选托管；不新增模型准备层或假模型。
- [x] 4.2 显式 override_model 使用原实例，不先构造随后弃用的配置模型；保留执行路由、工具和 Thread 校验。prepare_agent_model 继续服务原执行组合，不为此重新拆分 instruction producers 或已有模型查询服务。
- [x] 4.3 定向验证压缩分区／profile 与原规则一致、忙碌 Thread 准入和 override 路径；复用已有模型发现／测试独立性证据，若其源码未受影响不另建验收矩阵。

## 5. 定向验收与复杂度复审

- [x] 5.1 经真实 Gateway、隔离工作区和 Workbench 验证工具列表／详情、本地失败及正常会话操作；验证目录／详情及内部 reader 共用原 catalog、无合成身份；更新当前目录后，同时验证管理读取取得新值而原调用的 search_tools 仍消费冻结定义；仅其他 cursor 输入不变时续页成功，策略／readiness 等输入变化仍按原冲突拒绝，当前执行资格继续生效。原查询失败明确返回，不退回旧执行装配或另一范围；补验证本次改动触及的模型／上下文消费者，其他 Skill／状态查询保持原实现，不重做全页面或六编辑器专项工程。
- [ ] 5.2 工具目录暖 API 与本地失败点击至错误 DOM 各采集至少20个未筛选暖样本，冷进程样本单列；保留规模、源码指纹、request_id／执行关联及 p50/p95/max，遵守既有500／1500／3000ms预算，超预算先归因，未达标如实保留未完成。
- [x] 5.3 集中完成受影响静态分析、定向合同和集成测试；前端或协议消费者有变化时完成真实类型检查及 Web build。测试使用进程外超时／内存保护，保留原始输出和退出码，补核管理 schema、ToolSet 冻结值及真实发送请求的公开参数约束一致，保留必要 Provider 格式转换而不要求 wire 文本逐字相同，覆盖原 extra 策略／隐藏字段／字典 schema；复用已有 schema 缓存与冻结测试，不为验证每层函数调用次数而新增脆弱测试。不默认全仓长跑或重复取绿。
- [x] 5.4 更新涉及文件的已有 AGENTS.md，检索确认旧模式／伪身份／重复定义和悬空调用清零；仅新建源码目录时补四节说明，不新增总结文档。
- [x] 5.5 从修改前重新推导最小方案，删除多余 wrapper、Prepared／binder 框架、重复状态／校验／缓存、无必要生命周期接口与测试；按查询入口表检查没有新增同视图 reader、第二派生／匹配／分页器、联邦调度或聚合缓存；按三类迁移判定逐项复核原 Skill／CSM／上下文／摘要／状态及搜索 owner 未被重复实现，核并发差异，仅补验受影响部分，不自动同步 dev、重启或提交。

实施验收记录：代码与契约检查见 design 的“实施证据与未通过项”。5.2 已采集完整样本，但暖失败 p95 超预算，保持未完成；不能用目录延迟或局部测试替代。

2026-11-22 用户要求先修复已记录的附带问题，暂不处理点击发送至错误可见的延迟。附带修复及验收见 design 第三轮记录；5.2 保留原预算与未完成状态。

2026-11-24 重新经隔离 Gateway、原生 Workspace 和真实 Workbench 验收，原件在 `out/tests/temp/2026/11/24/capability_latency/artifacts/`，受进程外时间／内存保护的命令及实际退出码见 `validation-receipts.json` 和各轮原始日志；`source-fingerprints.json` 记录 HEAD、Backend 源码和 Workbench 产物 SHA-256。两组完整样本使用相同 Workbench `main.js` 指纹 `e35bc5229b6e...`，请求均有真实 `request_id`；发送样本还保留 `job_id`、`turn_id`，浏览器页面异常为零。

- `run-3/capability-catalog-result.json`：目录 66 项、99,755 字节，冷读 704.1ms；20 个未筛选暖列表 p50/p95/max＝28.6/47.9/54.7ms，20 个暖详情＝25.0/34.2/43.9ms，工具面板 355ms 可操作。满足完整 API 的 500/1500ms、首次 3000ms 及界面 2000ms 预算。
- `run-4/capability-invalid-result.json`：无效 custom factory 的真实发送点击至新 Turn 错误 DOM，冷样本 1531.6ms；20 个未筛选暖样本 p50/p95/max＝1057.2/1283.8/1587.4ms。冷样本满足 3000ms，但暖 p95 超 500ms、最大值超 1500ms；5.2 保持未完成。
- `run-5/backend-stages.jsonl` 是独立 1 冷＋5 暖分段诊断，不与完整样本混算；此时并发构建已将 Workbench `main.js` 指纹改为 `56d24d75e722...`。暖发送 HTTP 为 478–786ms，其中 `JobService.start_job` 为 394–654ms；随后 runtime acquire 为 194–319ms，内部 assemble 为 172–252ms，阶段有包含关系不可相加。下一项最有价值的优化是在 Job owner 内细分消息准入与持久队列派发，再按同一真实入口验证；不以工具准备局部耗时替代完整预算，也不在本变更改动其他会话的消息存储或导航。

同日继续验收的原件位于 `out/tests/temp/2026/11/24/failure_ui_completion/artifacts/`，索引为 `delivery-receipt.json`。真实 Gateway／Workbench 沿原 Job 发送入口阻断模型 SDK、模型构造和图编译，无效 custom 声明仍在列表末尾；每批均保留 1 冷＋20 个未筛选暖样本、请求和执行身份，页面异常为零。固定 Workbench `main.js` SHA-256 为 `2a0045fca9e0494f7b2e8b8e806db787112e27278abf99d44a60972dae8ad208`。

- `acceptance-01`：冷 1602.1ms，暖 p50/p95/max＝984.3/1164.3/1573.3ms；`acceptance-02`：冷 1421.6ms，暖＝884.2/1079.1/1269.3ms；`acceptance-03`：冷 2222.1ms，暖＝989.7/1229.6/2092.8ms。仍未达到暖预算，5.2 保持未完成。
- checkpoint 输入读取减法后，`acceptance-04` 完整 1 冷＋20 未筛选暖样本：冷 1195.3ms，暖 p50/p95/max＝791.0/937.9/1166.8ms，页面异常为零、源码前后指纹无变化。冷与 max 满足预算，暖 p95 仍超 500ms，5.2 保持未完成；不能以延迟下降代替验收通过。
- `acceptance-02/capability-catalog-result.json`：66 项、99,525 字节，冷 583.7ms；20 暖列表＝27.4/35.5/38.5ms，20 暖详情＝21.6/31.7/32.9ms，面板 303ms 可操作。目录验收通过不能替代失败 DOM 预算。
- 已将队列恢复改为消费同次 SQLite 读取的原 record、DTO 和真实 revision，删除逐项重读和 claim 前后的无消费者刷新；FIFO／generation CAS、原取消与事件顺序保留。118 项 Job 单测、24 项 IO 单测通过；终态槽清理后 44 项取消相关单测、5 个真实取消参数通过。失败装具和暂停旧预期的原件、归因及修复后结果均保留。
- `acceptance-03` 分段继续显示 checkpoint 输入重复读取。当前已将原输入读取与 checkpoint 消费合为一个受保护 worker，直接返回同次 canonical 消息和 admission；恢复只读、取消排空原 worker。checkpoint 12 项及执行编排 32 项单测、Ruff 通过；真实取消集成 13 项通过，含用户／内部输入读取与消费阶段的四个重复取消参数。旧装具补真实 claim 后再通过 8 参数，删除死端口后的队列终态 13 参数通过。工具恢复暴露事件分页冷读只查缓存的真实缺口，已改为原 Turn 锁内复用完整恢复入口，真正缺失仍明确报错；完整证据、structured 原 block 和后继先领取后消费的 19 参数已集中通过（`checkpoint-integration41`，37.54s，受影响源码无漂移）。最小复审同时删除无产品调用的 `accepted_job_outcome` 实现、协议与 mock，相关断言直接检查原权威队列 record 的终态。

- 主线程进一步将原队列 load 收敛到 raw records／同次 revision，已有 Job 复用已核对输入，仅未知 Job 沿原 project 冷恢复；144 项 Job 单元通过，新增两参数验证冷 load 读正文、暖 load 不重复及 identity 冲突拒绝。`queue-integration42` 三文件真实集成 40 项通过（77.25s），覆盖冷恢复、删除／取消／暂停、FIFO／generation 及写失败重试，11 个相关源码与测试前后指纹无漂移。修改 9 文件定向 Ruff 通过；全 app Ruff 有 230 条原有告警，与同文件 HEAD 同规则比较新增 0 条，不作为全 app 静态检查通过。原件见 `delivery-receipt.json` 与主线程 `provider_preparation_v2/artifacts/app-static-16/`。
- 同原真实负载 `acceptance-05` 完整 1 冷＋20 未筛选暖样本：冷 1105.9ms，暖 p50/p95/max＝705.4/855.5/1205.0ms；页面异常为零、21 个唯一 request／Job／Turn 身份、固定 Workbench 指纹及源码前后指纹无变化。退出码 1 仅因暖 p95 仍超 500ms，5.2 保持未完成。原分段暖输入与 admission 读取各由 60 次降到 40 次；start_job_prepared p95＝182.9ms、message_http p95＝272.7ms、checkpoint_input p95＝211.2ms、acquire p95＝185.6ms。阶段有包含关系不可相加；原件为 `acceptance05-stage-summary.json`，没有重采或筛样。两轮结束后已交还集成门。

- 同一性能链继续删除 admission 重复读：按 Job 查找与完整校验共用原只读连接；用户 canonical reader 在同一快照取得原 admission 与 root，原用户／内部消息 reader 统一返回二者，MessageService 和 checkpoint 消费者删除前置重复准入读取。没有增加缓存、已核可选参数、第二 reader 或兼容层；三 queue 接口保持，internal notice／seed／derived 的原 preimage 与引用校验、claim 先后及受保护 worker 取消排空保留。`input-loader-unit44` 的 113 项通过与 45／46／47 的定向补验 9 项共同覆盖 122 项相关合同；修正 fixture 的根 ID、workspace 及真实 claim 前置，不放宽原断言。notice 3 参数另由主线程 `pending-notice-19` 通过。最终 22 Python 文件语法及 diffcheck 通过，Ruff 仅 1 条 HEAD 原有 RUF059、新增 0；原始失败、命令和源码指纹均留原收据。生产切片冻结，随后真实输入／取消集成完成，未重采性能，5.2 仍未完成。

- 2026-11-25（北京时间）恢复后补验：`integration57` 10 passed／124 deselected／65.50s／exit 0，覆盖真实编译失败、取消排空、child归属及工具观察失败后的终态收敛，全部 app Python 与三份集成测试指纹无漂移。`acceptance-07` 仍用固定 Workbench、1冷＋20未筛暖样本：冷1106.4ms，暖 p50/p95/max＝603.5/796.6/1351.3ms；页面异常为空，21个唯一 request／Job／Turn，源码指纹无漂移。exit 1仅因暖 p95 超500ms，5.2继续未完成。阶段暖p95：message_http＝269.5ms、acquire＝235.4ms、assemble＝164.5ms、failure＝102.5ms；阶段包含关系不可相加。准确命令、原样本与收据在 `out/tests/temp/2026/11/24/failure_ui_completion/artifacts/`。TODO：根据独立提交／装配栈继续定位，不以提前拒绝无效factory、跨请求缓存或放宽预算替代完整点击到错误DOM合同。

- `input-loader-integration49` 真实队列、取消／恢复、internal 事务中断和三种附件输入共 55 参数，53 通过、2 旧 `admission_read` 屏障未进入；原始失败及 71.35s 退出码 1 保留。屏障已迁到同一 protected worker 内的组合 canonical reader，未改原重复取消／holder／终态身份／事件计数断言；`input-loader-integration50` 原 4 窗口全部通过（8.66s，退出码 0），合并覆盖 55 个唯一合同。两轮相关源码前后指纹无漂移，修改测试 Ruff 通过，结束后明确交门 Luna，无本代理运行或排锁。

- 进一步的 `targeted-profiles08-09` 各3个样本仅作诊断，不替代上述无profiler的07验收。`profile-scope-selfcheck10` 已复现cProfile把无关worker／协程计入当前profile，累计时间、调用次数及调用边均不能用于精确归因；原件仍在同一 `failure_ui_completion/artifacts/`。TODO保留：以未筛暖p95＝796.6ms为有效未达标证据，后续使用具备正确执行归属的观察方式定位；本轮不据污染数据删除正确性校验、不另建性能采集框架，也不将5.2标为通过。

版本读取纵向切片：真实归属的3样本短诊断发现，`snapshot_version` 只需要版本却调用完整队列快照、准入与输入校验。现沿原SQLite revision提供精确读取端口，由Session编排按原规则聚合main／child；不新增缓存、表或版本账本，真正load／claim／dispatch校验保持。4项定向真实反例5.19s、完整Job单元144项71.69s、三文件真实队列闭包37项96.61s均exit 0且相关源码无漂移，收据在 `out/tests/temp/2026/11/24/failure_ui_completion/artifacts/revision-{focused12,job-unit13,queue-integration13}*`。同负载acceptance08的1冷＋20未筛暖结果：冷943.8ms，暖p50/p95/max＝617.9/753.0/1069.7ms；页面异常为空，21个唯一request／Job／Turn，源码无漂移，exit 1仅因暖p95仍超500ms，5.2继续未完成。07至08的下降不能据两轮推定稳定收益；完整原件见同目录acceptance-08及acceptance08-stage-summary.json。诊断使用临时直接计时，父归属／to_thread／无关Task隔离由scopecheck12核验；探针逐调用落盘有开销，3样本及async线程CPU不作性能门或独占耗时结论。

消息流首次订阅删除固定120ms等待，改用微任务，沿原AbortController撤销同步StrictMode探测与卸载订阅，没有新状态或重连分支。真实浏览器StrictMode观察2次setup／首次cleanup且仅1次存活订阅，卸载后原signal已abort；新增生命周期反例、真实类型检查与完整Web构建通过。消息流全文件25通过、2条文案失败在修改前完整HEAD archive原样复现，未改预期。原件见同目录`latency-microtask16-delivery.json`。acceptance09关闭全部阶段／timer／wire探针（DIAG=0、PROFILE=0）：1冷966.6ms，20未筛暖p50/p95/max＝576.1/747.5/949.2ms，21个唯一request／Job／Turn，页面异常为空且源码无漂移，exit 1仅因暖p95超500ms；全部21次消息流上游200，无早订阅404。07／08仍有每阶段同步诊断写盘，不把09作为相同配置的精确收益对照；5.2仍未完成，约110ms终态长尾也未证明消除。

后续两个独立切片继续保留原预算：用户输入与队列准入共用原 SQLite 只读快照，真实入口 8＋8 对照从每次两次物理开库降为一次，暖入口中位数 14.932→10.119ms；232 个不重复合同通过，收据为同目录 `shared-input-snapshot17/delivery-receipt.json`。终态 ChatTurn 的 memo 比较补齐原消息流展示字段，避免 Job 已失败但后到的失败帧被同 Turn revision 挡住；两个真实组件反例先失败，修复后 memo／response／history 共43项通过，最终 fixture 补齐后两反例复验通过，真实 tsgo 与独立完整 Web 构建均 exit 0，原件为 `memo19-*`。无新增缓存、状态或订阅链路。

`acceptance-10` 复用新构建、关闭全部诊断探针，完整1冷＋20未筛暖：冷1070.2ms，暖p50/p95/max＝585.5/1216.3/4344.4ms；21个唯一request／Job／Turn，页面异常为空，源码及产物指纹无漂移。暖样本17的发送HTTP本身3309.4ms，样本18为814.7ms，均保留、不筛除，也无证据将其归为环境问题。exit 1，5.2继续未完成；下一步只对 Job 创建发布及原持久化监听边界作有归属的短诊断，不以正确性修复或局部耗时替代完整性能门。准确命令与原始退出码见 `perf-acceptance10-receipt.json`。
