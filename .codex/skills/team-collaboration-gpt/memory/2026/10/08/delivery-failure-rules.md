# 按任务查询的交付失败细则

主代理维护的条件性规则，来自当前 goal 已记录事故；具体候选、原始失败和验证效果以[实时台账](team-collaboration-state.md)及其收据为准。此处保留先前入口中的技术细节，避免每次恢复加载整份事故叙述；规则不能替代当前源码、合同和实际证据。

按切片检索相关关键词即可：wire/参数/认证、CSM/cutoff/fork、生命周期/关闭、路径/index、协议/重启。只将适用且能改变本轮决策的规则带入派单，不将所有历史约束叠加成新前置。

A05-L2/L3固定tree `785919ada5a83f65cdbd565218424ecd87966dad` 增审各发现一项P2：停机owner对非空desired一律拒绝，合法删除非末成员必须先启动旧组；非managed删除先删registry，再执行可失败的SSH转发清理，502时target已丢而转发残留。前者仍由同Sol修唯一owner的完整离线退出/恢复合同；后者由Luna在独立数据盘代码worktree只修路由非managed片段及独立故障测试。在线成功与最后成员退出不能替代停机非末成员/外部清理失败的验收；按真实状态和副作用顺序补受影响差异，不重跑无关历史。L3两文件窄修及独审已通过；L2四文件35核心/21相关边界/1真实Gateway及固定增审通过，仅说明对应缺口关闭，不推算模型倍率。

L3两文件核心已绿后，作者继续重建手工patch且压缩后重复恢复产物；该patch还停留在前一版路由，新增测试不在普通git diff中。已纠正为停写文件/SHA/raw最少交接，由主一次固定树导出与严格重建；禁Git写的作者不承担patch生成。主本轮一次read_thread把完整turn输出带回造成额外恢复开销，后续用compact wait，只有具体异常才过滤取相关工具项。两类尾部开销分别记账，不据此推算模型速度倍率。

L2真实Gateway停机DELETE发现publication持久遗漏：内存target已删且剩余owner已保存，SQLite仍保manual target；原publisher `_save(owner="system")` 让store按owner合并时保留了缺席manual行，JSON夹具没有暴露。修同一publisher受控DELETE的完整snapshot/CAS范围，并在真实GatewayStateStore重开核删除与其它owner/sibling保留；不另加SQL删除writer，不把内存列表成功当持久成功。同Sol修同一publisher后真实SQLite重开、CAS旧引用与真实Gateway验证通过，固定增审无finding；失败raw保留，不把成功外推为所有存储owner保证。

A05停写后，作者活树`git diff <基线> -- <路径>`报告check0，却漏掉24个尚未入观察index的新增文件；主固定162源码＋4文档后发现其中两文件EOF空行。原对象和诊断保留，作者仅删2字节、AST一致、Ruff通过，未重复动态测试/build；最终固定树check与重建由主补验。活树diff0不能证明完整候选，格式小修也不应触发整批重测。独审的show/grep为固定读取示例，固定preimage→tree的精确diff同样合规；不要把示例变成多余审批门。

A05-L1固定独审发现最后成员退出合同未闭合：schema拒绝空desired，组操作强制保留发起成员；旧DELETE/registry.remove却仍直接close，绕过已实现的drain和force interrupted持久化。重启/组切换绿灯不证明全部关闭入口迁齐；同一生命周期闭包须核CRUD删除、最后成员、runtime替换和恢复caller，并用合法输入验证，不能只检查新controller方法。由同Sol修核心，Luna不插手大改；效果以最终固定增审和真实入口收据为准。

A05-C1补核回滚失败测试后撤回存储主题早期无finding：保primary异常和cleanup note不等于连接可继续使用；测试手工rollback解毒掩盖了store仍对业务开放的坏态。窄修由Luna独占两文件隔离失效store，不加自动重连；主独审并给Sol固定补丁后再跑最终核心。正常rollback成功应仍可继续事务，清理失败则明确拒绝后续访问，两者分别验收；尚未交付前不记修复有效。

C1两文件固定tree `f136a8665abd92cc0542f02610872db43305088f` 已主独审/严格重建、作者额外源码0、合ROOT供Sol最终组合。作者selector `write_transaction or read_transaction` 为10pass却漏新 `test_transaction_closes_store_when_rollback_fails` 的4参数例；主只补精确node，4pass/0.80s、raw/SHA保留，Ruff0，合14相关用例，覆盖声明已要求更正。read/write统一清理且仅3方法AST变化，不把这次窄修外推为全库所有失败路径保证或整个A05完成。

A05-L1修订首验run002为95pass/3fail：两例真实恢复时重分配辅助端口、改变同一旧generation完整manifest；一例fixture漏service_urls。原raw/SHA已主核，失败保留；同Sol修原地址恢复与真实身份/健康proof，不放宽断言，最终结果仍待停写。

A05-C2按已证明的同类边界窄查：固定f136的SessionControl rollback失败后公共connection仍暴露未提交数据，原测试手工rollback掩盖。固定方法AST内存探针actual0、raw SHA7983f9e91bcd180fc275d89ca1edf9390ec31b41f42a4c24471bf3ede35e956c已主核；同Luna只在已有代码worktree改两文件，不影响Sol核心/ROOT动态验证。它证明连接隔离缺口，未证明生产数据事故；指定完整文件4pass/0.79s、targetRuff0，主独审与固定tree d825严格重建/作者额外源码0已接受；保body/真实COMMIT后续写、close成功/失败的原异常身份和公开read/write拒绝。目录Ruff未改I001保留，ROOT合成待Sol停写，不外推全库保证。

A05-L1修订并发验证沿旧helper读改写同一attempt列表，主观察条目7→6，原唯一命名raw仍保留。作者按实际工具退出/各raw机械补独立收据，后续串行；避免以清单覆盖重跑已绿测试。每次收据独立写后再汇总，不把台账序号当运行身份。真实组合原3核通过，新2例因历史追加日志累计数和离线target投影预期失败；只在生产依赖未变时复用3核、定向重验新2例，最终结果仍待停写。

A05-L1真实Web016在reload后仍寻找Gateway控制面locator，实际已回会话工作台；actual1/224.16s，旧cancel/join/blocked/force/stale/remove phase完成，errors与route_stubs均空（作者JSON）。driver显式重入控制台，不改产品或删旧判据；现正式单例没有独立剩余入口，沿最少既有用例重验，不临时重构测试框架。Web017补验actual0/232.06s、1pass，原全部phase及新empty/DELETE判据保留；导航修正已验证。继承SHA比较以d0 fixed tree及已授权EOF correction为准，不能用更早162manifest把已删LF误报新语义漂移。

## A01：真实激活、单正文 detail 与 Skill 恢复

本节只在相关闭包使用；默认正文加密已由用户取消，历史密文/AAD规则不再约束本地detail。完整原始事故可查实时台账列出的外部历史快照，不恢复为实施前置。

- public8 静态和机械重建通过仍漏 fork/import 的 privacy 引用，收集实际4；整批下线前检索全部直接/间接caller。批量替换去digest又拼坏相邻SQL列名，删比较字段又留下无条件拒绝；统一核同表DDL、SELECT/INSERT/fork和剩余比较条件，再验真实入口。边界59的34fail中27处共用assembly fixture漏thread_id；直接AST只找到一个shared helper，还须核snapshot_factory的**kwargs及子进程crash程序字符串。一次迁齐公共fixture、storage/reader旧签名与子进程catalog生命周期后，61对应完整privacy/toolsets/seal三文件及受影响domain共799pass、actual0/151.185s。窄效果已验证，不能外推全部间接调用或其他change。
- 内部ToolSet精确序列化不能按默认API分类裁正文；默认投影也不能通过聚合hash/revision/cursor外投正文指纹。内部封存恢复和默认投影分别验。plan hash排除操作ID需包含嵌套ref/selection；正文、来源和ToolSet变化仍须影响hash，detail owner/ref由seal/restore独立校验。
- default-tools提前创建无resolver Skill，令Factory正确版本被跳过；收口唯一producer。ToolInvocation执行前await emitting model-call同一注册链，并锁内封存canonical call。resolver用messages/item_catalog精确JSONL locator，不能依赖展示message ID、latest model-call或current Registry。
- checkpoint typed manifest正确不代表payload roundtrip正确：单tool-call被恢复为tool_calls数组而改变hash。仅保call/invocation/attempt/result生命周期ID binding，从真实可见name/args/result恢复原payload，不复制正文、不放宽typed/hash比较。主曾误判raw ID归属，完整SQLite/JSONL反证后撤回，成本归主。
- 新三模型调用fixture的8s是通用helper预算，无已确认产品SLO；实测正常终态后该case用20s，通用默认8s、业务10s与原语义断言保持。30s诊断暴露真实canonical冲突，不能当绿；cProfile外层0/raw failed仅作诊断，直接pytest验收。
- 领域边界已出现baseline同字节陈旧fixture：omitted ToolSetIdentityRef被当ContextRef、将plan_id误列hash语义、贡献摘要常量与现行preimage不符（旧常量由来未证）、误认为request_only_ref(content=...)会保存正文。按现行规范迁测试，保持owner拒绝/正文禁存断言；hash用手写独立preimage与跨语言SHA证明，不能抄生产输出更新golden。

- R01真实测试的同步spy在async路径改成_aprepare→_project_prepared后失效：projections=0不能据此判来源丢失；先联核真实producer/compiled结果和正式SQLite seal，再将观察点迁共用边界并保完整后续reload/retry断言。R03的CSM control与activation派生revision先按各自owner核规范，不能以旧同名key直接套用。
- attempt48的23个fallback失败均为同一`mcp-tool-catalog:v1` required snapshot缺失，业务断言尚未到达，actual1/153.05s；fixture只构造publisher登记descriptor而未publish。集中修公共publisher/fixture后，attempt49的成功＋取消和两处恢复case共4pass、actual0/17.677s，验证通过共同准入；随后完整fallback50为31pass、actual0/18.377s。保required gate与业务timeout，不用默认alias、删断言或增大等待求绿。两处恢复测试相对6d1的36/8个assert AST未变；不同组选例不据耗时宣称总体提速。
- lifespan54主体1pass但fixture teardown进入已由lifespan关闭的catalog，actual1。55为actual124/240.059s、raw仅`..........F`；先前按点数归lifespan的判断撤回，不能据此证明具体失败或generation覆盖。56具名首个compiler用例FAILED后teardown卡住，actual124/120.056s，尚未到lifespan：fixture仍mock旧`get_mcp_tools`并期望调用1次，真实caller只读MCP Owner `publication_snapshot`；失败后的手工lease未finally释放。迁真实publication/schema/tool/generation精确同源断言，用既有ThreadRuntimeLease异步上下文或finally清理并保失败报告；产品非会话DTO的真实getter caller仍保留，不把旧spy失效当死接口。54已证明的生命周期owner迁移保留：该case fixture不预启动MCP/registry，main.lifespan同task执行真实start/shutdown，普通fixture继续自己管理；保原关闭/closed/reactor断言，不加产品closed分支或重开catalog。核心57完整3pass、actual0/39.469s，验证publication同源、真实lifespan及Skill重启；整组58以actual1/200.48s正常终结，93pass、一处generation断言差异，证明已不再卡teardown，不能外推全部lease转换或完整业务闭包。

## A02：runtime source、同 Session child、retry 与 internal fork

仅在本批对应链路使用；实际attempt和证据定位在实时台账。

- H1无Turn准入不得套normal item acceptance；自身执行输出须有精确view membership，不能为恢复放宽公开history资格。checkpoint与stream shadow的tool call仍按现有typed carrier唯一替代规则合并。
- Goal callback已持owner锁时传锁内冻结DTO；Terminal source提交不能重入Session gate。completion claim/finish必须带原completion_event_id和command_start_sequence，typed uint64贯通Node/proto/transport，保原generation；持久claim失败恢复仍须独立进程验证。
- internal Runner也按真实execution创建/回收scope，不fake Turn。首次唯一runtime准入由既有选择owner冻结实际provider进canonical preimage；retry读同wakeup原值，不pop canonical metadata或重选provider，不改resource unload规则。
- empty/custom/delegated/question保同execution真实retry；route不得提前吞掉最终空响应，question恢复保kind/reply_required。业务确认失败须由同owner持久化linked execution/model-call/assembly，不由cleanup降成execution_lost/unknown。成功retry曾漏早期assembly收尾，后续集中核心已验证四类retry所有assembly terminal；保每次已提交model事实，模型记录绿不能代assembly收尾。H6另验证model-call尚未提交的sealed assembly按execution_lost冷收尾，不补造Provider响应。
- internal context_fork仅按唯一branching合同的精确checkpoint/view/execution/admission选择来源。request-only seed须复制其冻结contribution/detail/selection/prefix，全manifest联动映射target-local身份；seal/fork共用原索引writer，不从当前文件/seed补正文，不沿notice或公开history资格筛选。
- 目标未发布时detail path/publish只接受本次精确session/thread/creation record的prepared定位，正常读取仍要求已发布catalog。联核创建、正文发布、catalog/journal的失败收尾，不提前发布或放宽通用resolver。ToolSet hard rebase继续携带同owner前次sealed selection/frame的合法untracked来源，不能只重建tracked集合。H4真实核心、H5独立Saver冷re-fork/发布与finish硬退出、H6真实backend两次冷启动均已分别通过；最终完整候选另验。
- Gateway两例启动exit3的正式stderr均为健康身份不符：生产读取JSON的workspace_id（backend UUID），generation_target_stub无backend_workspace_id时只回data.status。先核启动stderr和真实caller，用原健康合同迁齐stub；output_workspace_id是Gateway展示身份，不新增backend_uuid同义字段、不放宽生产gate。failure-closure01中原GeneratorControl/navigation启动已通过，failure03中catalog聚合/offline/restart也通过；原失败整组actual1仍保留。
- 真实Provider team E2E01两case在apply_patch/create_team前均因emitting model_call_id缺失失败。pending-writes的assistant带vendor resp_* ID、无response_metadata.model_call_id；stream已持原注册UUID，final carrier却另写无model身份group。此前H6 Chat stub靠lc_run--前缀而未触发。由SealedAssemblyDispatchBridge在本次handler返回边界绑定原callback_model_call_id，保vendor ID、structured/Extended command与业务内容，校既有身份冲突和ToolMessage配对；不改require gate、不取latest。20个定向用例含同步/异步真实TCP已通过，E2E02原身份错误消失后在下一请求暴露reasoning/extension projection拒绝。原checkpoint group支持既有reasoning-carrier/v1，history却整体拒绝extension；解码下沉既有mapping叶函数供两者共用，未知schema/version仍拒绝，reasoning26核心通过；真实E2E03首次HTTP因额度不足403、只有1call/assembly，后续profile拒绝为次生错误，不能按旧错误未出现声称已越过修复。原stderr与冻结状态独核后记外部UNMET，停同前置重复；完整真实复验仍待。新增测试过窄地漏status=completed导致两TCP在第二HTTP后仍红，按完整原carrier校保真，不删合法字段；native_request没有Provider能力上下文时保既有显式loss，不擅扩跨Provider回放功能。带vendor身份的窄绿不证明完整真实Provider历史闭合。

- 直接Reasoning checkpoint边界暴露真实生产错误：accept_turn已把新canonical root纳入get_tuple current view，按同message_id早返会把首次写入误判幂等，并绕过stale task/pending_sends控制清理。用户幂等使用原acceptance owner返回的权威事实；已有message不重复append，首次user acceptance或待清理控制仍走唯一checkpoint更新。internal notice/seed先由原admission durable，不将source Human/None追加graph；bool只表示graph是否写入，无待清理控制时可为False。reminder保canonical item及精确producer/execution/SessionThread、request可用与public/history/graph隔离，不按旧条数改预期。完整20例和少量真实HTTP/Runner/取消恢复6例已通过；fresh Saver只证明新对象从磁盘回读，不冒称关闭或独立进程重启。

## 按业务检索的既有细则

A05 新真实进程用例的 attempt004 漏配置必填 `supports_reasoning`，启动阶段失败；005 将既有冲突 409 写成 400（1 passed/1 failed，140.51s）；006 将原候选退出映射 502 写成 500（1 passed/1 failed/1 deselected，168.18s）。三份 raw/SHA 已核，耗时是整次测试耗时，不全算无效等待。原因是测试输入与异常期望未先核当前合同；修正新 fixture/断言，保完整成员、generation、恢复和隔离验证，不改生产码迎合测试。006 单成员/陈旧合同已通过，修正 502 期望后的 007 到达真实恢复断言，暴露下述生产故障；测试合同错配已解除，不据此宣布整个 change 通过。

A05 attempt007 的真实 SIGKILL 恢复：新进程已按旧完整成员/generation 启动，但 controller 无条件取消旧 drain；目标已 ready，返回 409 后整组被误标 offline。由同 Sol 修唯一取消链，消费真实 lifecycle：draining 才 cancel，ready 与 accepting_jobs 同时成立则已恢复，其余状态显式失败。008 actual0、1 passed/2 deselected（88.64s），保 502、双成员健康、同恢复 PID 和旧 generation 的正式收据，raw/SHA 已核。这只证明该修正的真实恢复效果；随后同 owner 候选句柄/CAS/aux 清理闭包还在改变，相关核心须在最终生产组合重验。

A05 attempt016 actual1（608 passed/10 failed，229.89s）的新 CAS 用例暴露真实 publisher 原位污染：提交前改了已公布 target 与 `runtime.service_urls`，失败虽替换 registry 指针，外部持有的旧对象仍含候选 URL/generation/lease。主从 raw 指出旧引用差异，Sol 确认生产原因并保原断言，采用独立 staged target/dict 与整体映射替换、失败恢复原对象，完整迁 caller；不把 expected 改深拷贝掩盖问题。提交后观察者异常另保真实已提交事实，不能拿旧 URL 对新组 cancel。018 actual0、8 passed/76 deselected（pytest 1.90s），raw/SHA 已核；真实 SQLite CAS 保留直接旧对象/映射引用和持久化无污染断言，亦含提交后观察者与未发布候选清理边界。只验证这八项；bootstrap 后续两入口和最终真实组合仍待验，不将早期三条核心扩大为最终 CAS 验收。

退役目录审计首报将 `out/tests/unit/gateway/runtime/` 测试镜像父目录误列为运行数据叶，还把本轮未执行的 `/proc` 未发现结论归给主线程。主检查子项为 `test_process_*` 等，拒绝删除此项；真实 `/proc` 又发现 3 个旧 E2E aux/backend 孤儿进程仍打开已删除日志/SQLite，按 uid、cwd、argv hash、start ticks 和单成员进程组核归属后，pidfd SIGTERM 均观测退出。报告要求撤回错误事实、保原版本并只列真实 run-* 叶；源代码、正式 artifacts 和独有补丁保留。清理效果以主 `retired-directory-cleanup/` 收据为准；不存在的观察不得写成已做，basename 和整树大小不能代替可删目录的内容证明。

后续 Wire 审计正确列出四个正式镜像 workspace 叶并披露未核进程条件，主独核 fixture/源码、保留边界及进程后删除，约 269.9MiB。但主派“十树中最多四项”仍诱导全目录遍历，作者又宽搜旧 tests，输出截断后才收窄，任务约 551s；不能据四叶判定准确便宣称审计流程高效。后续主直接给已知候选或从 tests 路径映射，先交行动结论；搜索范围规则效果尚待实际派单，当前不另造全树清理任务。

A05 截至 write-web-build-269 的实际命令收据有 38 次完整 Web build（其中 21 次对应 `.test.` 文件），另 7 条 `bun build` 是单文件语法检查，不能混算。38 份 raw/SHA 已核，Vite 自报构建阶段合计 724.58s；不是完整命令耗时，也未证明每次生产依赖完全相同。原因是用户已要求整批迁移，派单却未明确把逐文件 build 收口到批末，作者仍机械遵循旧逐次检查习惯。主已明确同闭包批末统一 build，编辑保快速静态；后续完整交付效果待验，不能先报提速倍率。

A05 021 Gateway 621pass、022 受影响 Web 123pass（均 actual0、raw/SHA 已核）之后，024 真实浏览器仍 actual1，成员合并未成功。主已核真实响应 join 200/blocked、两条 proxy_stream，以及“完整组正在切换路由”被记成已公布组的 connection_error/offline；原 JSON/SHA 保存在 `real-browser-attempt024/`。作者最初归因 workspace_proxy，进一步追到真正 writer 是 `RegistryDtosMixin.list_dtos`：锁内构造生命周期结果时，健康探针经业务 resolve 被暂拒，再汇总 offline 并 mark_backend_error 污染已公布 owner；撤回代理持久化归因。具体调用因果与修正效果待固定候选独审及真实复验。由同 Sol 一次闭合已公布合同的健康探针与页面观察流释放/恢复，保唯一 owner 的 generation/digest proof、all-proxy 引用约束和原组成功断言，不让 Luna 插手核心或将 expected 改 blocked 求绿。

上述修正后 025 批末 Web build、026 DTO 39pass、027 观察流/重连 34pass、028 真实 Terminal 1pass、029 真实 Web 1pass 均 actual0，raw/SHA 已核。浏览器 join/force/removal 成功，独立 secondary SSE 仍阻挡 safe，force 关闭旧流、陈旧 generation 409、pageerrors=[] 均保留；只能证明这些范围，完整候选独审尚未完成。新闭包统一一次 build 的执行已观察到，完整交付速度倍率仍无可比依据。

A05 030 actual1、4pass/6fail（83.88s）：旧真实编译夹具整段替换 `build_runtime_for_agent`，只返回 model/system_prompt/model_routing，漏现行 instruction_producers；首个 Job 失败后四个 Goal shutdown 用例又分别超时。新显式候选把同进程日志设成 DEBUG/WARNING，触发已有正确拒绝。作者先下沉到模型构造仍不完整：033 因 binding 缺 kwargs 失败，035 完善 binding 后又超时；不能继续逐字段搭 fake ChatModel。最终只替换 LiteLLM 外部 acompletion/aresponses 传输，返回完整流与请求 metadata，真实 runtime builder、模型构造/binding、sealed dispatch 与说明来源全部运行；036 actual0、1pass（26.80s），四 Goal 未再次展开同一红前置。随后 037 整文件 actual0、10pass（88.06s），raw/SHA 已核；作者报告涵盖真实 Event/Trace/MessageStream/background/catalog 锁、四个 Goal shutdown 和各 mount 独立 Agent 说明候选 proof，固定候选独审仍待。未弱化进程日志 gate；动态窄门与整组迁移效果已有具名证据，不外推所有 Integration 或整个 change。

冻结参数须核真实绑定和发送结果：框架会按 structured-output strategy 改写或省略 `tool_choice`，adapter 还会强制 `stream/include/store`。直接抄 `ModelRequest` 字段曾在固定增量早审中漏掉这些事实；先修提取再接封存，不等完整 HTTP 验收才查。 Auto策略fixture还须核框架模型名兜底，profile=False未必选工具策略；按实际策略提供HTTP输出，不将错配fixture归生产回归。

- **交付残缺、漏真实入口。** 手筛hunk曾漏签名、删除和Web caller。用[交付校验脚本](../../../../scripts/verify_delivery.py)从固定树严格重建，核blob/hash及候选外源码；调用清单包括API/UI、CLI/进程启动、恢复、协议、间接kwargs消费者及实际分发的resources/skills和提示模板。A04生产/API验收后才发现gateway-context技能仍教模型调用旧resource/expected_revision/locator；运行时说明也是直接caller，须随唯一工具schema迁齐，不能把它误当纯规划文档。同类必填签名一次迁完，核真实callee再跑代表入口；不存在的Saver属性曾阻断28项，旧签名曾让15项连续失败耗时299秒，不能加alias求绿。完整模型E2E前先核真实路由状态码、异步回执和各窗口可见控件；主测试误设enqueue200、误点扩展窗口隐藏会话树，曾到末端才暴露并重复整场景。迁移规划也核原输入/输出和正式用例：曾漏图片读取、文本补丁删除/移动，新增能力延期不能废除已有能力。可行动增量先交，未知及后续范围分开，不等全量调查才回报。主独占验收候选固定后合并通知一次；状态消息不新增实现前置，作者不重复承担已分配的测试独审。连续通知曾引出重复读测试、下一生产增量延迟，收窄后的效果待实际交付。
- **合同与基线错配。** live、旧HEAD、固定候选混读曾误判接口。先辨Git对象ID与文件SHA256，后者不能交cat-file；一次误用曾引出无关环境调查。tree没有提交历史；tree到worktree比较须用该tree初始化的私有index或直接blob，错误index曾让主把未变fixture报成删除。唯一提供者先交完整合同和依赖，消费者待实现字段不能阻挡取得前置。闭集只在owner登记，消费者具名引用；同步清旧spec/验收要求，严格校验不能发现全部语义矛盾。early包可先审，必要caller闭合才集成。
- **身份、writer和跨载体约束漏项。** Thread/Session混用、内部通知伪造user Turn、取消后未收to_thread writer都曾过helper测试。核真实user/internal/child、HTTP/WS和恢复入口，只消费已校验admission；prepare与封存读取须消费同一规范checkpoint namespace，曾漏传namespace而查错owner，缺失记录必须明确报source-mismatch并保原因；幂等比durable原请求。首次final与checkpoint重复提交、重放和fork须联验同一model-call身份；codec从消息自身取权威值，显式caller或持久字段冲突必须拒绝，不能放宽typed provenance guard。typed字段核owner/slot/revision CAS及未封存约束；descriptor/root policy、prefix/ToolSet与public/protected binding须联校，各自hash合法不足以通过。
- **未知结果和镜像脱节。** 确定拒绝漏回执、以资源存活证明动作完成、释放失败先丢句柄均曾污染owner。原业务owner在真实转换边界记录结果并同步镜像；未知副作用继续阻断，按精确身份/revision/终态核实后结清。回滚或释放失败不得发布verified/closed，普通列表不能清unknown。传输授权只接受精确入口的专用no-effect拒绝和同身份、工作区、verified、非倒退revision快照；一般409/超时不能当确定拒绝。
- **生命周期被缓存遮蔽。** LRU驱逐仍被服务持有的catalog、关闭listener漏派生writer、卸载await窗口接受新claim都曾出错。唯一registry持有到真实释放；写前消费同一closing准入，先收finalizer/后台writer再关catalog，失败保重试句柄和generation fence。backend unload依赖aux时先完成backend关闭再停aux；整组同时TERM曾令revision同步失败、owner未知和Application shutdown failed，safe restart保aux的成功不能代此验收。核精确owned进程组的活成员，根PID退出不能证明子进程已退；root-only检查曾让真实关闭绿仍漏子进程。测试清理也从首个进程起覆盖，按原组回执重扫；child尚未登记时依赖child PID会漏收，须覆盖根已退/子回执未写的故障窗口。超时强杀只证明回收，不能证明drain/unload完成；原handle保失败，已退出后重试也不能洗掉事实，不能增大预算求绿。测试重启的新handle交原fixture管理，不能被临时function scope提前回收。
- **路径与索引误操作。** 相对编辑、默认cwd、裸Git和自建archive/.venv反复错根，私有index只隔离暂存。每命令固定绝对workdir/index/产物路径；并行校验各用独立index，write-tree会更新缓存并取锁，曾共用私有index令验收启动失败；复用指定完整WT，不/tmp、不复制整树。长产物路径曾使Chromium的Unix socket启动失败；Linux可用持有目录fd的短`/proc/<owner_pid>/fd/<fd>`引用，先探针核真实目标，全部子进程退出后才关fd，物理产物仍留正式runtime/tmp，不新增工作区副本。检索限已分配根，大型/不确定搜索带外部超时；无界rg扫/曾卡交付近九分钟。共享index从absolute git dir定位，变化先比mode/blob/stage；没有精确备份不称精确恢复，重复误操者收窄写权限。
- **验证不代表固定交付或实际发送。** live绿灯不归缺装配的固定包；保存每attempt raw/实际exit及源码指纹；tree参数从当次manifest读取，手填错ID曾令测试未启动。管道和陈旧baseline标签不能代验收，Web build不代tsc。Web失败先收全部实际窗口/iframe的console、失败请求与错误控件aria/title；只截主窗、只读图标文本曾延迟定位ERR_NETWORK_CHANGED。先用不调用模型的实际加载探针缩范围，不把integration的route.fulfill搬入E2E，也不凭网络失败改生产。参数提取也先对照原流式/非流式入口与省略参数：共享helper曾强制非流式发送stream=True并重新带入None采样值，两个真实入口早验证实回归；同一作者修正后4项及相关33项通过，完整wire仍待验。真实Chat转换曾把合法overlay变空：核实际Provider转换和HTTP正文；request hash含规范化wire与projector/version/profile、工具、附件及loss/redaction，不能用LangChain envelope代替。非Agent直连由真实构建入口显式标记用途；缺admission或native projection不能当作豁免，否则会放行漏绑定的Agent。字段排除只作用已识别的认证/传输/attempt载体，不能递归删schema或正文同名键；schema声明由真实wire入口路径及合法载体类型识别，不能仅凭任意嵌套properties或type=function/json_schema形状豁免，曾连续返工造成正文丢字段或凭据绕marker。分派wrapper不得绕过统一字段名/marker校验；曾逐wrapper手写字典循环只校child，六处直接凭据漏拒；匹配须覆盖现有认证header别名及大小写、连字符形式。合法声明保留，实际值须拒绝或显式脱敏；同时验合法schema和同形业务正文，域探针不代实际HTTP验收。seal幂等不承诺HTTP只发一次。fixture对照domain/callee、实际codec必填字段及冻结preimage，保golden、原输入形态/断言及Provider opaque字段，不加默认/timeout求绿。Provider漂移输入须成组改变有效profile、声明与参数；只改旧字符串会提前落到source-mismatch。同epoch换profile须真实rebuild边界，主fixture曾违反该限制导致两红；首次封存、同epoch拒绝与合法重建分开验证，不放宽guard求绿。applied断言随唯一owner迁移，不能恢复已退出真实链路的第二writer。测试和生产来自完整一致组合。参数化真实服务测试还须隔离每个参数的持久active配置与控制面状态；只改JSONC会复用旧endpoint，曾让Chat访问已停的Responses替身。拒绝零写入要比完整checkpoint/config/metadata和原始持久字节，不能仅比ID。
- **诊断跟踪改变时序。** 全场景ptrace曾把真实PTY停止从2.96秒放大到10秒请求超时；超时先在相同真实入口对照有/无跟踪，原失败保留，移除跟踪后再验完整场景。进程外timeout/内存保护继续保留，不增加生产timeout或删除业务断言求绿。跟踪限定必要进程与阶段，不能将被放大的时序直接判成生产缺陷。
- **CSM模拟提交掩盖边界。** 旧测试伪commit绕过实际seal；迁后手动restore直接seal旧epoch即冲突。边界验收走真实view、checkpoint-versioned CSM与pending transition owner，首个成功新epoch seal才consume/applied；不能给fixture假epoch默认或跳source联合校验。共享view入口须逐boundary核副作用：曾把只读history_replay映射成rewind，倒改tracking并登记伪pending；历史projection不得恢复CSM或触发执行epoch；只查put曾漏put_writes，再漏rewind两入口，所有同步/异步写API须在同一owner guard拒绝history selector，并联验读取仍可用及完整持久状态不变；泛化active-view条款曾与history只读语义冲突，公共spec/design/caller一起修订，不能只改实现或追加任务；只查pending行不变会漏active branch与pending归属交叉；旧Turn/execution诊断probe不能证明合法继续失败，须由accept_turn/replay_as_new_turn取得真实新Turn后联验seal/reopen及pending生命周期。目标checkpoint仍可见的snapshot/untracked正文须从旧sealed selection/detail携带；只按tracked batch重建曾在早审暴露正文丢失；cutoff前携带/后消失已由真实边界验证，效果限该范围；同checkpoint更早anchor曾复活snapshot：来源前沿由唯一seal冻结，boundary冻结cut，不能用后来admission改变的view或root wire排序重猜位置。正文选择与latest_visible须同cut同事务对齐；只裁ref曾让r2正文移除后仍被标为visible。root提升inclusive/before/complete、r1/r2重启及完整epoch/typed ports40项、seal fixture13项已通过；真实before-anchor replay已通过新admission→seal→restart retry，归位三路径独审及两真实边界亦已接受；仅复用这些具名范围，不外推完整R验收。来源fixture区分完整SKILL.md与解析正文，真实冻结入口缺frontmatter曾挡住六场景，先验证共同fixture再展开。失败回滚沿真实seal写面一次列齐正文/detail（含孤立行）、投影、selection、sealed commit/cursor、完整plan/ToolSet绑定及dispatch；关联查询或只查plan两列曾连续漏半提交断言。合法unsealed plan/compact registry比较预存状态，rollback后独立failure audit具名排除，新正文/detail仍在内存；提前登记detail再标tombstone不是回滚；正文先prepare再同seal登记发布。拆分后曾因登记要求未发布父目录存在而阻首次seal，须验fresh owner与publish/commit失败，继承tombstone期待同步迁完整无残留断言。compact草稿provenance仍按已定合同允许，保既有行不变并具名核新行无绑定，不把全registry不变误作原子性。fork须联核selection contribution与ref provenance的映射，并从pending/consumed/continued真实状态复制后seal/reopen；普通fork绿灯曾漏领域引用冲突和view membership，修后本次三phase通过，静态前驱/source身份闭包另验。严格领域ref变更须验比较、持久化恢复与retry；omitted仅保身份时不能重造缺正文的public ToolSet，完整wire ref校验仍保留。单个合成fixture红灯也不能独自证明生产缺陷，须核实际caller。
- **目录正确仍越过职责。** Saver seal拼Provider请求、runtime做wire映射、函数内导入隐藏双向依赖，消费测试全绿仍未发现。固定差异早审核最近AGENTS职责/顶层依赖，报告标题或整包manifest不能代实际审查范围；委托一个子状态不代表本文件无直接I/O。纯转换交现有mapping/provider owner；不放宽AGENTS或保重导出双轨。读snapshot内恢复detail曾触发初始化写锁：先冻结plan/ref，快照外取不可变detail，保owner/revision/hash和准入。
- **崩溃测试漏持久化窗口。** importer只测完整临时文件，空/截断next仍阻恢复。覆盖创建、部分写含UTF-8、fsync、原子发布和另一进程重开，区分未发布与已发布损坏，不删审计求绿。正常退出不能证明强杀关闭安全。
- **恢复漂移与诊断过早。** 错schema tree、未核caller或仅看上层role曾误派缺陷。恢复先核基线/候选/报告根、真实类型、构造注入/MRO和完整链路；低层测试漏前置不等于生产漏前置，无caller/规范依据的假设列未验。反证成立立即撤单；旧生成数据、继承缺陷和主协调错误分列，不归错当前模型。
- **协议与重启权威遗漏。** 把Browser二进制帧当JSON、SQLite只存投影漏owner元数据、新客户端漏认证都曾失败。核精确路由的依赖和例外，再按真实协议/认证验收；overview 等复合读取需启动其实际辅助服务，不因通用代理印象改断言或加空数据默认。辅助进程沿生产launch与最近AGENTS选择运行时，曾用Bun启动Node PTY服务导致立即SIGHUP，不能据此误派生产修复。退出码须核运行库信号约定与实际lifespan/unload收尾；Uvicorn正常返回后会重抛SIGTERM，不能一律要求exit0，也不能仅凭-15证明成功。引用和authority由同owner事务保存，重开核完整对象/游标、失败零副作用，不以初始化或mock替完整产品入口。

恢复先读当前坐标及相关收据，历史台账按任务查询，大型JSON只取本次相关键。未变规划与候选核指纹后复用已验收据，不能每次恢复全文重读或重新展开完整检查。当前队列及时移除已完成动作，历史attempt不标为进行中；曾残留已验Web场景的“第四轮运行中”，与最新收据冲突。台账只记录范围、结果和调度的实际变化；文档提交后的HEAD由外部收据记录，不为追逐文档自身坐标重复提交。

审查按固定tree、路径和实际运行依赖复用，只审新增差异及受影响入口。先确认旧报告确实审过这些实现路径；前置清单或fixture差异审查不能充当生产实现接受。消费者窄审附原owner已定合同的精确定位与本次未验边界；漏给合同曾令独立HTTP全等断言被误当待定，不能改弱断言替代生产闭合。曾因重复调查已完成的问题或反复追加报告收尾检索而迟迟不交结论；先交已有发现与未验边界，精确行号打磨不阻挡可行动回报。反复扩查可显式中断，同会话只派报告交付；一次报告专派已实际交付，原未证边界保留，不能据此宣称总体提速。关联缺陷单独登记，主线程决定后续归属。依赖核查包含fixture的初始化、装配和恢复callee；直接owner blob相同不能证明整条运行链未变。依赖有差异时按真实输入判断影响，明确保留复用范围与未验项，不把静态差异自动变成整包重跑；DDL还须核初始化实际导入的SQL，不能只比入口函数。阶段报告归档核本轮运行身份和来源更新时间，未触达阶段不复制上轮产物充数。参数层规则变更须联验实际preimage生产者、封存与恢复入口：调用方包装层不等于wire参数层，不能用递归识别同名request键补豁免。主未写实现的窄增量由主独审，复杂公共事务用只读审查者，不为每包重复等整队。严重失败先纠正任务，再窄改技能；后续真实交付验证后才记有效，不叠同义规则，也不把继承缺陷和主错误算作当前模型返工。


## 整批调度、A03 清理与 Luna 窄修的具体纠正

- **小包调度阻断完整实现。** 反复扩租约、冻结、同步与重跑造成依赖等待。一次授权整个调用闭包，先迁齐生产和核心公共fixture，再核心修通、集中边界，最终只冻结一次。后续队列曾漏8.12/8.14/8.15业务owner；按原需求补齐整批归属，不能用批次摘要或“最终验收”替代剩余实现安排。A01完整包已交付，生产净减1109行；新增归属仅准备，尚无实现或同起点模型效率对比。

- **观察入口通过但生命周期漏接。** A03固定独审发现分页唯一性、cursor认证、稳定错误及删除恢复遗漏；同作者完整修订后分页P2关闭。再审发现expiry GC仅凭时间强制cancel仍active/settling的lease：到期测试只覆盖已完成操作，漏运行/恢复holder。期限关闭重试，不能证明holder停止；原owner先提交结果或可核验取消，恢复owner先证明旧holder停止，再GC。修订5/7/126/6项具名通过，固定增审确认此边界，却发现正文与terminal lease分事务删除的崩溃孤儿。清理也须由原owner同一事务完成，核心同时核中途失败、重开、正常全清及非终态拒绝，不新增永久去重或孤儿扫表。最后原子清理10/2/249项具名通过、固定增审closed并集成；只证明这些生命周期边界，整个goal仍未完成。

- **小修夹带新能力或缩窄合法输入增加返工。** 正文隔离首版增加无需求的Python哈希与tagged系统，取消后直接deepcopy又拒绝原JSON合同接受的MappingProxyType；普通dict绿灯遗漏了有效输入。局部复制须按既有JSON owner接受的Mapping/list/tuple处理并保hash、serde/replace，不另造冻结框架。异常修复同时核adapter和消费入口，transport保错但channel再吞会留下原缺陷。窄修按已复现事实完成直接闭包，避免扩成核心重构；当前正文修前红/修后4项绿，独审/集成按台账记，不外推模型效率。

- **批量迁移失败后扩大写入。** AST字节偏移错误留下空路径，Ruff无参数扫全仓。迁移非零即停止；静态修复只接收非空且属于闭包的路径数组，AST按UTF-8字节定位。范围外68文件已独核恢复，预防措施后续效果待验。

- **路径与索引偏离仍发生。** 每命令固定物理cwd、私有index和out产物根，不/tmp、不整树复制。新私有索引先read-tree固定基线再列未跟踪文件，否则会误报全仓。只读候选输入不得写副本；窄修曾在快照cwd找测试、误复制8文件，已核hash清理，改用明确代码worktree及独立正式输出；阶段切换须读最新租约，不继续旧准备阶段。重复违规审查会话撤Git权限；共享index字节变但stage不变，不当源码吞改、不恢复旧index。新隔离纠正效果待验。

- **本地正文加密增加恢复和测试链路。** 用户取消同目录AES、两个key及thread HMAC，A01已删除；保单份detail、完整性和默认脱敏。联邦、SSH、认证及Provider opaque reasoning仍归原owner，不迁旧开发数据。


Inspector 后续单复现接收故障只写 runtime.error_message、pending caller 却拿通用关闭错误。只在原 receive_messages 传诊断 RuntimeError 并以捕获对象作 cause，保 traceback、正常关闭/取消分支与原清理。固定2路径 tree379258f4，专项1passed/0.22s、主固定Ruff/AST/严格重建实际0，作者树差异0；原timeout文件虽未事先hash，主已从7d基线blob逐字确认还原，不重新造preimage。作者又从业务workspace cwd跑pytest、触及旧共享测试且bare status；已纠正到独立测试和代码树根，Git禁令覆盖status。不能因D/??恢复源码。前期探针无持久raw不计红测actual；具名成功不外推真Node或模型倍速。

- **固定审查输入未及时交付增加核查成本。** 主给95ab任务却允许读仍在写的ROOT，information字段已被Sol删掉，审查者无法复核原preimage，且扩大一次全树只读rg。主冻结原Git对象及manifest后重锚报告；合同判断无阻塞，未审生产。派单前提供本任务完整且最小固定输入，区分preimage、生产候选与规划快照，不能事后把活树SHA写成95ab。

- **生成器先删输出导致级联红灯。** A04 Buf远端插件不可用时，脚本先清4个generated根，原绑定需按固定preimage恢复（实际142files）；NoPythonFiles、OpenAPI/import和Web缺文件失败不能计绿或当独立业务缺陷。完整生成应先写out指定暂存根，全部步骤成功后替换；插件版本/实际生成未验证不能手改generated或恢复别名求绿。原生成attempt及后续修复效果按台账记。

A04最终独审收尾曾重复触达另一主审的联邦主题，且可行动结论先于完整报告。主明确每人剩余主审边界、停止扩查后，两份固定报告交付，均findings=[]；只证明本次交付收尾有效，不证明审查可整体省略或模型倍速。下次先按主题分主审，跨主题引用证据，短结论立即交；无需为了所有任务表格或目录统计延后核心批次。

Luna 后续 RemoteObject 优先级调查在复用已集成的真实 Node primitive throw 证据后无新 finding 收尾；request timeout/cancellation/receiver 生命周期静态与已有测试亦无具体新候选，无探针、动态测试或源码分支。只证明这两项停止条件执行有效，不能外推 Node 全面无缺陷或模型速度差。既有证据够判就停止，避免为常驻人数制造重复调查。

A06前置只读复核发现原文件树“不实施”登记把目录GET的3样本median16.26ms写成mutation p9516.3ms，并将源码推断的重载/无丢失更新写成实测。原raw只证明GET；create/refresh数据属于Session catalog。主撤回“已验证无瓶颈”登记，不改原阈值，补mutation级量化后再判10.4c，Gateway10.5须自己10.1证据。只证明口径错误被发现、队列已纠正，后续量化/实施效果待验。证据：TEMP/wire_contract_reviewer/artifacts/a06-navigation-conditional-evidence/report.json与原measure_tree_m200.json。
