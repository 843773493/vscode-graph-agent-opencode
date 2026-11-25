## 1. 冻结接入合同与现有owner

公共派生身份、缓存前缀／工具授权、共用循环、调度与恢复由 `unify-derived-execution` tasks实施；本清单同名项只负责压缩接入和业务验收，不重复派单，已验收基础证据保留。

- [x] 1.1 核对normalize-semantic-items的真实format3、Item／消息组、工具闭合与fork最终合同，列清所有自动／手动压缩、预览、模型路由、来源和请求封存的直接／间接调用方及现有测试；只更新本change对应设计落点，不新增总结文档。
- [x] 1.2 对照实际配置继承、模型元数据及统一错误合同，确定唯一typed准备输入／结果和预算证据字段；与并发owner协调schema／协议写入，不复制loader、CSM或错误协议。
- [x] 1.3 核对真实发布支持来源和rollout结构版本，登记候选元数据归属与原数据库结构变更；无基线不推测旧格式兼容，不预占版本号或转换真实开发数据。
- [x] 1.4 按design真实代码缺口表确认Graph入口、BackgroundTaskRegistry／SessionControl、summary_compiler、MessageService、replay和history.md全部消费者；确定唯一派生准入、候选结果与active view权威，避免新增任务／消息状态副本。

## 2. 实际模型预算与只读规划纵向切片

- [x] 2.1 复用原Provider／Model resolver和冻结投影，完成包含root、tools、sources、历史、附件／媒体的预算；区分输入／总窗口、真实输出预留、I／C交集及R未知、容量状态与计数质量分离，不增加上游输出限制。
- [x] 2.2 在Workspace既有context域同步compaction策略、CompactionPolicy及schema／内置默认值，校验0.55／0.80／0.90和两种应用策略，沿实际extends解析并接入auto_summarize及下一准备热更新。
- [x] 2.3 将原手动预览接入只读规划，移除为了查询构造模型／Graph的路径；迁齐真实API消费者，保留scheduled与已应用的区别，不新增并行控制API。
- [x] 2.4 定向验证同名跨Provider容量、完整schema／媒体计数、unknown、auto_summarize关闭时95%仍可发送／overflow不自动补压缩、热更新和预览无模型调用；核真实配置加载与序列化，不只测手填metadata的helper。
- [x] 2.5 沿现有projection compiler抽取共用只读计算阶段，预算／能力／最终root与tools计数使用同一冻结计划；禁止先seal再测容量，模型实例和元数据绑定同一配置快照。

## 3. 单一完整覆盖摘要生成切片

- [x] 3.1 将安全规划迁为语义Item／消息组／工具闭合边界，保留必要新输入、保护载荷和尾部；去掉HumanMessage或整Turn作为强制唯一切点的限制。
- [x] 3.2 在现有summarization生成链实现冻结输入、逐块全覆盖、有序合并与缩减检查；接入共同派生模型预算及独立身份，不触发自身压缩；禁止实际工具执行而保持允许复用的来源工具schema。
- [x] 3.3 实现一次绝对任务期限、32次总派生调用预算、最多三层合并与一次外部失败重试、冻结模型及在途／已终结补尝试期限区别；完整处理能力不足就明确失败，不做部分来源成功。
- [x] 3.4 全量迁齐并物理删除suffix-only截断重试和错误字符串伪成功路径；复用原安全投影，禁止静默剥媒体、解密受保护reasoning或合成工具结果。
- [x] 3.5 定向验证前段关键约束不丢、三块全覆盖、单块失败、空结果／工具输出、可分单项的分段全覆盖／不可分大单项、合并无缩减与预算耗尽；断言真实派生请求输入及最终覆盖manifest。
- [x] 3.6 接入unify-derived-execution提供的共同派生准入／模型入口，将candidate、块／merge及attempt绑定原压缩业务计划；公共typed引用、冷reader和发送链迁移归该change，本项验证真实摘要调用身份及冷恢复。
- [x] 3.7 压缩按共同合同只读冻结范围、保留可复用root／工具定义并尾部追加指令，执行工具集合为空；核主CSM／工具尾部／usage隔离，删除压缩私有tool_free／visible_text试探；公共缓存与权限实现不重复。

- [x] 3.8 迁齐_summary_instruction、分块／合并及compaction_summary注入文案，共用冻结范围语义；去除掌握最新会话和无条件继续旧下一步的假设，沿原提示词工厂／请求身份冻结。验证后台append改目标、后块明确完成旧待办及阻断不暗加尾部，不以精确文案快照代替真实输入／消息角色验证。

## 4. 候选冻结、持久化与幂等应用切片

- [x] 4.1 沿原rollout owner新增最小候选记录／读写端口及必要schema变更，正文和保留引用复用原存储；不建立全局库、独立生命周期账本或通用DAG。
- [x] 4.2 实现真实来源范围冻结、append可复用、范围编辑／删除／rewind失效、实际读取source变化失效；无关source变化只重算最终预算，避免以整体revision一律失效。
- [x] 4.3 实现最多两份可用候选、S1→S2直接依赖及扁平覆盖，区分S1+B实际输入与A+B扁平覆盖，支持S1未应用／已应用两种真实selection，互斥选择；无新增内容且目标不变不重复，真实窗口缩小时允许一次S2(S1)有界提案，仍不足明确失败。
- [x] 4.4 在原compaction提交事务原子消费候选、追加summary Item、切换view并登记pending transition；原source／seal事务消费transition，保持旧历史及assembly不变。
- [x] 4.5 定向验证append竞争、候选重复消费、应用后seal失败与重试、source相关／无关变化及S1／S2竞争；核真实SQLite事务、canonical selection和请求hash，不只测内存状态。
- [x] 4.6 原Saver用typed intent原子保存compaction view与checkpoint恢复绑定；迁齐Graph通道、MessageService上下文查询、replay、fork／rewind，删除_summarization_event数组二次投影及force／continuation控制路径，旧内存不得复活已排除成员。
- [x] 4.7 原文回读工具复用canonical provenance及原owner增量行范围索引，保留公开参数和有界读取；删除新history.md写入／全文重写及其依赖，旧原件不擅删、不双读，核重复压缩、失败候选、rewind后事件顺序追加、fork原文缺失及pruning保留行区间的分页行为。

## 5. 后台调度与取消恢复切片

- [x] 5.1 在原注册器接入版本化compaction handler，复用共同派生调度／恢复合同；压缩业务限制每Thread一个压缩在途、工作区一个后台摘要调用及4个等待压缩的Thread，不将其它purpose任务错误合并，公共registry实现归unify-derived-execution。
- [x] 5.2 接入80%提前触发、重复触发合并、on_demand／next_request与最终阈值复用在途任务；后台失败转首次阻断只使用原预算内剩余一次重试，重复准备不重置；前台不等待后台信号量，网络等待不持写锁。
- [x] 5.3 沿既有查询／Trace／统一实时通道暴露最小候选状态及真实错误；后台失败不污染当前Turn，不新增常驻连接、审批轮询或每请求完整性巡检。
- [x] 5.4 在共同生命周期基础上完成压缩候选／保留引用的中断、卸载、删除及失效处理；ready不持运行lease，晚到结果不得复活已删除候选，不另实现通用取消／恢复。
- [x] 5.5 复用共同派生隔离／调度验收，补核压缩后台中正常发送、同范围摘要复用、候选取消晚到、重启ready／generating及删除竞争；核真实候选与资源释放，不重复公共执行测试。
- [x] 5.6 将完整ready候选按共同结果先保存后task确认合同接入原rollout；核任务结果引用、唯一采用、崩溃后补确认／无结果不盲重放，公共发布gate与恢复实现在unify-derived-execution完成。
- [x] 5.7 接入稳定Item／工具／source提交后的合并观察点，覆盖回复后跨阈值但无下一条用户消息的场景；不逐delta计数、不新增定时扫描，描述符只保存非秘密引用。

- [x] 5.8 在原compaction scope接入真实后台生成等待反馈：默认80%按task去重逐次降5个百分点至60%，仅调整后续有效准备比例；核排队／失败／取消／失效／突发注入／fallback不误学，自定义target与prepare不越界、作用域／配置代际复位及普通execution不复位。原查询显示基准／有效值及原因，不新增持久化、定时器或第二触发阈值。

## 6. 所有模型与上下文入口迁齐

- [x] 6.1 在唯一model-call入口完成实际模型选择后的统一准备；普通发送、工具循环和无Turn内部调用均走此链，移除middleware按初始模型阈值的另一权威分支。
- [x] 6.2 接入实际fallback重新预算、候选优先及不足时阻断生成；区分纯provider_changed与真实compaction，正文改变创建新seal／request hash，容量不适合与上游失败分别归因。
- [x] 6.3 接入手动scheduled请求、消息／附件注入、source／ToolSet变更和中断续跑；保留原可信ingress、工具闭合、用户队列和取消语义，不复制各业务查询入口。
- [x] 6.4 fork目标沿原物化后在首次执行统一准备，原父view／未采用候选不复制或改动；rewind／编辑沿原owner失效候选，不移动用户anchor或扩大选定范围。
- [x] 6.5 真实执行入口验证400k→200k、不足候选只模拟不采用，补足后一次提交、强制保留部分超窗、unknown的有界overflow反馈及全部候选不可适配；核每次实际发送正文、模型身份、失败归属和可重建hash。
- [x] 6.6 手动API新增显式Thread目标且省略保留main语义；模型工具／HTTP汇入同一intent，子Thread前端携真实归属，tool result闭合前不应用；状态沿既有上下文查询／realtime区分准备、ready及实际采用。
- [x] 6.7 验证旧媒体合法摘要后能力降低、新媒体仍须vision、试过未采用模型不改view、最终source恢复导致预算变化、每次准备至多一次compaction及有界CAS重评；核主assembly／输入消费／前台usage不被派生请求污染。

## 7. 产品入口、性能与最小充分实现复审

- [x] 7.1 用隔离工作区和既有本地确定性模型装具，经实际Gateway／Backend／Workbench验收：提前生成不中断对话、最终阈值命中无新增摘要等待、无候选阻断及失败可见；截图与运行产物按现有out/tests规范保存。
- [x] 7.2 在50k／100k语义Item历史、有界active selection与候选范围下，以至少20次真实准备／seal记录完整p50/p95/max与未筛选原样本；验证热路径p95≤500ms、单次≤1500ms，冷读和上游等待单列，禁止全历史扫描或以scheduled响应替代完成。
- [x] 7.3 对实际修改文件集中执行相关静态分析和类型检查；若修改前端DTO／设置／状态展示，执行真实前端类型检查及 `bun run --cwd src/clients/web build`。所有测试带进程外期限和内存保护，不默认全仓长跑。
- [x] 7.4 复审并删除重复预算／任务状态、shadow history、第二组装器／发送器、旧预览构建器、旧阈值／固定65k触发假设／suffix重试、offset事件投影、Markdown归档写入及无需求兼容分支；逐项证明新状态／校验／持久化必要性，迁齐全部调用与测试后物理删除旧代码。
- [x] 7.5 核对实际差异、源码指纹、准确命令、退出码、未验证范围和前置变更归档顺序；严格校验OpenSpec，只有真实验收通过才勾选对应实现任务。

2026-11-24 旧入口迁齐补验：`test_session_short_write_admission.py` 19 passed，退出码0；真实 HTTP 登记原后台任务和候选，显式下一请求采用意图仅写原候选，main／child归属、完整checkpoint与canonical原文不变，未解析凭据或构造模型。删除竞争与历史结构边界补验3 passed，退出码0；空checkpoint不登记任务，删除中的Session在读取checkpoint前返回409。两批均带1000秒进程外期限与4GiB数据段上限，准确命令、HEAD及1329／1330项源码前后指纹分别位于 `out/tests/temp/2026/11/24/provider_preparation_v2/artifacts/short-write-02/` 和 `session-delete-and-migration-03/`，均无漂移。`history_failure_43` 3 passed／126.56s，退出码0，核真实工具日志、原生I/O与日志次错归属、冷读和后继恢复；原件位于 `out/tests/temp/2026/11/24/compaction_application_v2/artifacts/history_failure_43/`。这些证据不替代尚未集中验证的旧事件／fork消费者迁移，也不将受限或未遵循摘要指令的外部模型实测计为通过。

2026-11-24 fork接续收口补验：原文早段正文缺失后原行位置不变，后段仍按原line_offset读取；候选原子采用、S1→S2三种真实selection及原消息计数、预算模拟与最终wire一致，共7 passed／19.39s，退出码0，250项相关源码无漂移，原命令与指纹见 `out/tests/temp/2026/11/24/provider_preparation_v2/artifacts/compaction-count-11/`。Agent Context计数可得性和分页补验153 passed／61.53s，退出码0，15项源码无漂移，见 `context-count-09/`：不可得计数不伪造数值，诊断纳入每页字符预算，冻结snapshot冷往返及分片完整重组通过。公开proto已有optional计数字段，真实OpenAPI结构校验通过且两镜像一致，见 `context-openapi-10/`。这批局部证据不替代尚在补验的summary-only fork再次压缩与本地继任场景，也不勾选仍在集中迁移的4.6／7.4。

2026-11-24 fork接续正式矩阵补验完成：`fork_recompact_50` 3 passed／8 deselected／31.12s，exit 0；实际fork后冷initialize、目标两轮冻结／生成／采用、扁平lineage、原文ranges及裁剪后位置均通过，原件完整时raw计数6→8，summary-only原件缺失时返回null而上下文及真实HTTP查询仍可读。修复fork恢复消息视图把同Turn摘要误作完整Turn的写入条件，保留完整性校验且不复活仅作assembly backing的原根。定向源码指纹见 `out/tests/temp/2026/11/24/compaction_application_v2/artifacts/fork_recompact_50/command.json`；正式收据见 `out/tests/runner/run-tests/artifacts/2026/11/24/run-de87d15e4858/integration-structured-prompt-lifecycle/run.json`，相关源码稳定，全仓收据仍有无关并发修改，不能宣称全仓无漂移。结合原文区间和失败候选既有证据，4.7完成；4.6仍等待新发现的LangGraph冷恢复UUID合同补验，7.4／7.5保持未完成。

2026-11-24 模型选择与Graph冷恢复补验：手动HTTP使用会话已选择模型，模型工具使用发起Agent已有的typed模型引用；共用request在同一配置快照解析，自动／fallback显式配方继续沿原链。集中单元59 passed／34.26s／exit 0（`provider_preparation_v2/artifacts/manual-model-14/`）；手动重试及采用六参数通过（`compaction_application_v2/artifacts/manual_model_51/`），真实HTTP显式选择非默认backup_1/default并核透传的单节点1 passed／3.90s／exit 0（`manual_model_51/http_retry_02/`），三批相关源码稳定。采用checkpoint改用符合LangGraph任务身份要求的确定性UUID；正式Delta矩阵精确筛选4 passed／149 deselected／33.11s／exit 0，核同intent重复采用、同步／异步真实冷Graph摘要恢复、原events增量计数及历史selector，见 `out/tests/runner/run-tests/artifacts/2026/11/24/run-01f09059ba9f/integration-execution-history-delta/run.json`。这些结果修复前轮UUID阻塞，但尚不替代待补的epoch／Skill／资源及child工具消费者集中验收；4.6／7.4／7.5保持未完成。

2026-11-24 静态历史生成器与查询接续补验：生成器沿冻结范围、共同派生生成和候选采用链生成摘要，后续写入从已采用的 checkpoint 恢复消息及递增通道版本，不再手造旧事件或沿用压缩前内存。两种 checkpoint 写入频率共2项通过（16.82s，exit 0），逐个冷读首次采用后的全部 checkpoint，核旧成员不复活、原 canonical 身份保留及旧事件通道无写入；原件位于 `out/tests/unit/scripts/test_generate_custom_tool_rollout_fixture/artifacts/2026/11/24/resume-03/`。Replay 与 MessageService 定向45项通过（31.61s，exit 0），相关源码指纹无漂移；原件位于 `out/tests/temp/2026/11/24/compaction_continuation/artifacts/replay-unit-01/`。这批结果不替代仍待完成的真实 child 工具接续、完整427 Turn历史及 epoch／Skill／资源集中验收，4.6／7.4／7.5继续保持未完成。

2026-11-24 child 与完整历史补验完成：正式矩阵 `child_compaction_69` 1 passed／125.35s／exit 0，核真实 fork、backup_1 模型冻结、摘要实际采用、48行原文完整回读、工具按 emitting ModelCall 与 provider ID 配对、结果自身原始执行证据、main完整存储隔离、硬重启及双方续聊；14份封存请求审计通过，相关源码无漂移。`real_rollout_fixture_64` 完整生成427 Turn，资产合同通过，另一项仅旧 reasoning carrier 断言失败；迁齐独立 reasoning Item 后，复用同一完整产物执行两个原测试函数均通过（`real_rollout_fixture_reuse_71`，exit 0），全部 SQLite／JSONL／catalog／manifest 输入及源码指纹无变化，不将原失败轮次称为全绿。三测试文件 Ruff／diffcheck通过（`final_static_70`）。上述原件均在 `out/tests/temp/2026/11/24/compaction_application_v2/artifacts/`。TODO：完整静态装具生成耗时565.90s，第三组长历史准备阶段持续CPU计算，SQLite明显大于正文；以后按该阶段与表增长证据优化，不降低427 Turn规模或用较小样本替代本次完整合同。epoch／Skill／资源集中验收尚待完成，4.6／7.4／7.5暂不勾选。

2026-11-24 最终恢复及两处P2收口：资源 activation retention 全文件14 passed／18.56s／exit 0（16条既有Pydantic ReadOnly warning），此前唯一失败的 rewind × internal-null-turn 先单点1 passed／5.38s。前置改用真实内部准入及该执行的checkpoint，精确rewind到已含内部Item的来源；未改变生产可见性或放宽owner／hash／冷恢复断言。原命令、原始输出、退出码及12份生产／测试前后指纹位于 `out/tests/temp/2026/11/24/compaction_continuation/artifacts/resource-activation-02/attempt04.*`，无漂移。结合既有ready、child、427 Turn、Delta冷Graph、replay与生成器恢复收据，4.6完成。

本轮修复目标预算只参与提案身份而未送入摘要的问题：冻结完整未来输入的软目标及保留输入估计，沿原task input传入chunk／merge／retry指令；不新增API输出限制、目标重试或配额系统。另将在途S1结清后遇到真实缩窗的路径接入原唯一S2，两个等待共用同一绝对期限、不先采用不足S1，至多一次最终采用。SDK回放集成7个唯一参数通过（第一轮5 passed／2 failed，修正新增测试对registry查询语义的错误后仅补原2项并通过），原失败完整保留，不称单轮全绿；收据见 `target-budget-01/delivery-receipt.json`。最终runtime单元14 passed／6.92s／exit 0，生产和测试指纹无漂移，见 `manual-target-unit-04/receipt.json`。这些测试不证明真实Provider输出质量或可用性。

最小充分实现复审完成：原candidate／task／view／epoch／canonical owner各自保持唯一事实源；未增加SQL结构、影子历史、兼容分支、通用恢复机制或发送器。已全仓检索旧事件／force／continuation及旧期限名，生产零残留，唯一旧事件文字是生成器测试验证不得写入旧channel的SQL断言。修改文件Ruff和差异检查通过；前端类型／构建与协议生成收据沿既有相同源码指纹复用。完整装具565.90s的性能TODO继续保留，不扩大本次恢复链范围。
