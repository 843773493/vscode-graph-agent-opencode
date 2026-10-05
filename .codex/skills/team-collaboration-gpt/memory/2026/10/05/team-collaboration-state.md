# 团队协作实时状态与实测改进

主代理独占维护；稳定规则见 [共同流程](team-collaboration-workflow.md)。更新时间：2026-10-05 20:01（北京时间）。goal **active、无预算上限**，交接与七个 change 尚未全部验收。

本文件保留当前基线、依赖和恢复入口。已关闭阶段的完整台账可查文档提交 `e972590dcd0bbe4122aae3bce870e7f72894ed65`；原 raw/exit、固定对象和报告仍在各任务产物中，不为补日志重跑。

## 当前授权与路径

- 用户指定 **一个 gpt-6.1-sol/max 贯穿源码实现**，实际模型已核。`/root/sol_vertical_implementer` 是唯一实现者；Luna 实现者停写，`/root/candidate_boundary_reviewer` 仅按需只读审查。默认技能仍是无前缀 gpt-6-luna/max；本轮覆盖不等于永久默认，不为旧人数模板重复派单。
- 用户授权架构熵减、唯一 owner、删除源码开发生成的新旧数据、移除旧兼容，不再逐项询问。保普通源码、未集成独有改动与来源不明文件，不删数据掩盖 fresh writer 缺陷。
- ROOT：`/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 为软链接。
- TEMP：ROOT/out/tests/temp/2026/10/04/024121-team-execution；A：TEMP/coordinator/artifacts/sol-efficiency-trial。
- 唯一源码 WT：ROOT/out/worktrees/2026/10/04/024121-team-execution/thread_owner_implementer。**正在写 typed，不覆盖、复制、重同步或删除。** WT HEAD `ad093e0cdee2ca1225b1b9086db5e1d2c6b6b316` 不代表 live 或固定候选。
- 主唯一串行提交者；作者仅 freeze/export、不改 refs、不提交、不派生。每命令绝对 workdir/index、GIT_OPTIONAL_LOCKS=0；对象审查不读 live。提交与测试保护按共同流程执行。
- 三保护：app/gateway/control/generators.py、app/services/business/session_generation/service.py、examples/demos/Itemized_context_storage/。不新建整树副本/archive/.venv，不 /tmp，不复用迁移排除的历史工作区。

已裁定：assembly_ref=ResourceIdentity；unsupported_view；Gateway 创建拒绝保留 ID current；config layer=runtime_override，precedence 不变、active/pending 分开。delegate 使用 Session SQLite publication ledger 同事务，不造 Team JSON 权威；internal admission 无 Turn/accepted ingress，保真实 execution link。精确决定见 TEMP/coordinator/artifacts/owner-decisions.json。

## ROOT 已集成与真实产品

activation规划合同：`4e49b7c30c250e8eddd662e3014b10f4cb5c51f2`；术语收口计划：`81a084572e470ad2d017dc87ca355f1c0e02d934`。Browser主体源码 `11a925b0d6ba7f880517947d3b5207c0300cf3c9`（树 f5efe755c3b848d74a5c8da9c2f7ab8e30b558e5）；后续锁 c5a4、remote launcher e78c、远端UUID145759与正式WS helper5fef已串行集成，fresh连接元数据修复8989dbb2与重启认证consumer1de54734已集成，正式attempt7完整远端资源恢复通过。此前主体 0b355979（893路径）及启动器 c525e346 已集成。Browser 最终 111 路径严格应用并保留四份主独有文档，含 AGENTS.md；preimage、guard、双祖先与共享 index 均通过。

主服务 unit=boxteam-dev-cff6686cbc5b0e76.service，active/running，MainPID1274667；offset16，frontend8027/Gateway8030；BOXTEAM_HOME=ROOT/out/development-runtime/boxteam-home。恢复时重新核验。

授权业务工作区仅 ROOT/out/development-runtime/boxteam-home/boxteam_workspace 与 /data1/hyf/test_workspace/drive_bicicle。停写后 fresh 核两份旧无 schema browsers=[]并删除仅该空生成记录，普通代码和 identity 保留；证据 A/root-product-verification/browser-integration/obsolete-empty-browser-data-cleanup.json。

| 切片 | 固定交付与验收 | 证据（相对 A） |
|---|---|---|
| fork/Runner | fork请求意图4cf1d8、Runner847e：机械、定向测试与独审通过，已集成；sealed model-call/retry及itemized9.5整项仍待 | fork-intent-verification/、fork-intent-review/、runner-verification/、runner-independent-review/ |
| Graph/runtime | 36e084a8：固定范围通过并集成；真实编译 reasoning-only/retry随activation补证 | graph-runtime-verification/、graph-runtime-independent-review/ |
| WVL/catalog | 最终29513及完整9fbb已集成，registry生命周期/关闭准入与Goal/Job迟到writer闭合；不把继承Ruff红说成全仓绿 | wvl-catalog-owner-verification/attempt-2/、wvl-catalog-owner-independent-review/attempt-2/ |
| residency | 9e7013三路径早审通过：冷读不造slot、成功drain回收、失败保owner、claim落盘前消费closing；真实屏障finally窄修通过。durable-only shutdown与全产品范围仍待 | residency-stable-pre-review/、residency-guard/attempt-1/ |
| Terminal | 61812→9fbb恢复增量通过；真实PTY/UI/promote/stop/detach/delete及正式Session清理已验收 | terminal-owner-production/complete-delivery/、browser-owner-production/terminal-shared-recovery-delivery/、root-product-verification/terminal-product-acceptance.json |
| Browser | f9ac→b2完整包，b2→f838释放增量3路径独审通过；主strict/blob与原87+4收据分别核过。真实Chromium首轮19.53s/exit0：UI创建、grant/WS、导航/read、promote、正式Session删除、保留发现、无Session重连、原生stop/delete；page_errors/failed_requests均0，创建Session与资源已清理 | browser-owner-production/final-delivery/、browser-owner-production/release-confirmation-forward/、root-product-verification/browser-product-acceptance.json |

Browser 固定最终树 `f838d13ad91cbeaa13bc83eca41f324b3dfca451`。ROOT 产品验证只背书该临时场景；唯一正式 Web E2E 尚未验收，LLM documented-tool discovery 与 remote Docker Gateway Browser/Terminal 的正式尝试见下文，不能算整体通过。已绿未变部分复用，原失败 raw/exit 保留，各组不相加。官方生成后 tsc/build 已过；继承 Ruff 诊断的精确边界见各 evidence-binding。

## 当前 typed 切片

- 起点 UTC：2026-10-05T07:45:36.414759+00:00（先读合同）；baseline=f838d13ad91cbeaa13bc83eca41f324b3dfca451。
- 主已初始化状态 index：TEMP/coordinator/git/typed-canonical-status.idx，read-tree f838。作者产物：A/typed-canonical-production/。不得用 WT HEAD 或 ROOT 文档 HEAD 替代该基线。
- 原 itemized1.5/1.6 与 context4.6、6.7/7.1 的 typed/R01–R07部分：metadata→opaque namespaced/versioned extensions；ProjectionGroup、ItemProjectionIdentity、ToolResultEvidence、ItemSourceManifest、SourceBinding与显式relation贯穿writer/SQLite/JSONL/checkpoint/fork/history/provider。已有selection_role/replacement_policy/source_ordinal和RuntimeNoticePayload保留；source_ordinal只由owner-thread registry在既有ContextContribution分配/恢复。
- SourceBinding 已有下述早审树；后续 canonical 仍在途、未验收。作者报告四typed/关系与extensions、stream/tool终态writer、checkpoint codec/group、acceptance/source lifecycle、catalog无正文projection mirror已接；剩余fork copy/remap与v1 importer、carrier dedup/checkpoint authority、standalone history、attachments/旧fixture及真实R01–R07 producer/ToolSet条件roots。静态边改边跑；主不覆盖live，不把作者阶段报告当验收。
- SourceBinding早审固定树 5c285016bb0f32ce4aa8490760279c4c5c868767 于UTC08:33:56.630550冻结，43paths/28production/15tests；A/typed-canonical-production/source-binding-early/。主strict/blobSHA/AST43、78pass23.32s原raw与实际exit0/外置保护、保护0/sharedindex不变已核；CAS/指定默认披露路径的定点独审已完成，不背书全43path。该快照声明四canonical纯值定义未接线，canonical/R01–R07与完整sealed/fork未验收，**不集成、不勾整项**。从typed起点到该早审48.34分钟，不是完整切片耗时。
- SourceBinding阶段domain67与joint/registry54分别过，原120pass2fail、41pass13fail保留且范围不相加；imported seal source_ordinal序列化比较已作者修，未放宽owner。旧schema→10明确拒绝旧实验v2、原件不动；完整canonical尚继续。
- canonical首次领域全组actual exit124（180.022秒），原raw/command/exit保留；Sol确认共享fixture的visibility误写full，合法public/internal/private。同Sol修正后先单文件fail-fast，完整领域组与R01–R07仍待；这是本轮作者fixture返工，不归给基线CAS。
- 稳定早审与后续实现并行，后续只审新增差异；不裁残签名、callee、导入或消费者。不得据这条切片勾整个context6.7/7.1。
- 早审确认未闭合的revision CAS：5c285 registry仅按contribution_id更新，composition仅凭新replacement_policy放行，违反同owner/slot/revision CAS、未封存要求。主对比f838确认该无CAS分支已在基线存在，**不记为Sol新引入缺陷或完整交付返工**；同Sol已接真实caller→SQLite CAS、迟到冲突/immutable升级/已封存拒绝和内存无先行修改。定点独审已确认CAS为基线已有；默认assembly/history API无protected extensions可达披露，疑点已撤回、at-rest未验。10pass4.93s/actualexit0为live CAS专项，不当固定完整验收；真正sealed assembly拒绝仍待。

## 后续唯一队列

现行旧开发数据裁定已提交5c8e640：撤销旧跨Session team/board迁移物化，不撤销公开copy、SourceCopySnapshot、pinned、attachment claim/settlement与恢复删除门槛，也不撤销独立v1显式importer。唯一公开copy合同在checkpoint-context-branching规格；固定80b6独审与两change strict通过。

精确对象、任务ID与交付物由 A/implementation-queue.json 维护；A/current-task-index.json 是当前规划勾选索引，**不等于验收证据**。旧 fixed-candidate-task-inventory-29513.json 是历史规范快照，不据它复活已撤销的旧数据迁移或重建已有路由。

1. typed完整闭包先固定/验收，随后同Sol完成R01–R07与基线缺少的PrefixEpoch/root/ToolSet安全切换真实接线，再推进activation：唯一registry、持久workspace key/freshmarker、execution/model-call冻结恢复与原子sealbind；无None/当前源旁路。sessionVRN5.3 activation_bindings.display_uri/context_source_control_states持久新写/read-path仍待；真实编译reasoning-only→正文及耗尽retry、逐call/canonical身份和唯一终态补证。TEMP/coordinator/artifacts/activation-production-wiring/review-plan.md。
   - activation唯一合同已固定/独审/strict提交4e49b7c（tree1cc2）：snapshot_kind=execution|model_call，execution_id必填、真实userTurn关联/internal NULL，parent_execution_snapshot_id同execution；配置仍turn|model_call。同用户Turn retry/resume从唯一持久冻结owner复用原policy/turn-bound字节，internal在自身active slot冻结/恢复。9.1/9.2因旧Turn非空组件不满足当前合同已撤销整项勾选，尚未实现验收。
2. delegate：Session SQLite publication ledger与child publication同事务，recovery/factory/startcaller全接；published不倒删。
3. federated VRN/target-main：复用channel/grant/policy/credential和本地cursor，唯一authority语法；真实B⇄A⇄C、同本机authority等价、一次transit/两hop/deadline、最新权限/零副作用/不泄漏locator。**已有 GET /api/v1/sessions/{session_id}/main-thread，只接 WorkspaceSessionMainPort 消费者，不新增路由。** A/remaining-addressing-contract-review/attempt-1/report.md仅静态。
4. 按七change原task ID逐项证据，官方生成公共协议并独立tsc、strict OpenSpec、既有唯一 tests/e2e/clients/web/test_basic_chat_tool_loop.py 扩展完整场景、真实产品与清理。Browser里程碑不结束goal，不以组件绿灯或测试总数勾整项。
5. 用户 2026-10-05 新增术语收口：唯一术语表归寻址 change 的 requirement「术语必须区分机制、地址、身份与真实路径」；主线程按该 change 的 1.1/6.6 全量改术语和引用，与 Sol 在途接口错开。当前只登记计划，未开始源码正名，不保留 VRN/URI 别名；URI 字节、身份和授权合同保持既有语义。主线程的机械正名单独计量，不归业务补写或模型提速。

## 效率与协作效果

继续单Sol：fork/Runner/runtime/catalog/Terminal/Browser固定闭包已交，主补业务源码仍为0。fork首完整候选106.84分钟；旧Luna fork4pass17fail/33.8s来自不同合同与前置，不是A/B。Terminal和Browser精确开始时间未核，不倒推耗时；typed起点单列。没有可信速度倍率或永久最优结论。作者调用方遗漏、故障返修、fixture接手与主协调成本均记 A/efficiency-comparison.json。

远端Docker正式Browser/Terminal首次actual exit1、66.977秒（pytest65.21秒），bootstrap Web frozen lock失败，业务场景未开始。确认根锁和Bun1.3.11一致，Web lock遗漏早于本轮的@bufbuild/protobuf依赖（加入于277e4a96），不归因Sol；专用remote诊断锁已还原无源码差异。同Sol独立1path锁forward已集成c5a4c21，主严格重建f505、blob/hash/preimage、guard及双祖先通过，本次窗口共享index保持f2afa293不变；未混入111个typed在途路径。attempt2 Web frozen阶段已通过，但actual exit1/253.03秒（pytest251.36秒）：测试remote启动器漏BOXTEAM_RUNTIME_MANIFEST，Gateway启动即退出，就绪检查仍等完整期限。主从统一target入口读原stderr确定根因，不放宽生产distribution identity；同Sol在typed可停点后修既有gateway_target.py消费者及退出快失败，未启动另一个实现者。同Sol独立1path修复已交tree74e41/strict-attempt2通过，120个typed外源码差异逐path deferred；consumer probe5pass/0.56秒（包括退出无sleep/日志和健康重试），主Ruff0/严格重建/blob/preimage/guard/双祖先后集成e78c368，正式attempt3已启动。业务验收仍待；退出快失败已取得本地控制流probe证据，真实远端仍待。原收据 A/root-product-verification/remote-browser-terminal-attempt-{1,2,3}/，锁核验 A/web-frozen-lock-forward/root-integration/，consumer A/remote-gateway-runtime-forward/。

正式Browser documented discovery真实user Job在ROOT c5a4c21 actual exit1/26.03秒（pytest21.78秒）：Workspace AGENTS activation Source HumanMessage缺canonical execution attribution，provider prepare在LLM调用前拒绝；资源操作通过不能背书Agent完整发现链路。原收据 A/root-product-verification/browser-documented-discovery-attempt-1/。同Sol已接为后续R/activation producer验收，不放宽归属约束，不打断当前typed闭包，也不归为已交typed遗漏。

已验证改进：固定树严格交付，稳定差异早审与实现并行，未变审查复用；Browser确定拒绝回执与部分转换unknown分开，镜像同步放权威observe完成处，真实释放后才发布终态、失败保句柄。Luna仍适合本轮固定增量只读核验。screencast猜测经合法入口核查撤回，未要求作者盲修。

主错误单列：私有/共享index误识别约九分钟、title合同误判、Gateway路由ID与旧Session DELETE探针假设；原失败留存，不计为作者返修。路径计数须显式--no-renames包含删除/新增。ROOT共享index旧观察dfe47已过期，锁作者strict窗口后为f2afa293（变更原因未确定）；主锁集成窗口前后独立核验均为f2afa293，不能据此声称整个作者窗口未变。WT默认index最近观察e1cb50f；共享index由absolute-git-dir/index定位，继续fresh私有index，不还原共享暂存。

目录迁移已释放系统盘约238GB；旧根terminal-manager曾被2026-10-04的steering fixture重新写入，2026-10-05确认仅completed测试记录后再次清理，当前fixture已走out/tests显式UUID路径；授权旧数据与可复现副本已清，独有源码保留。原目录审查见 docs/handoff/20261003-151500-team-collaboration-directory-review.md。原始报告和提交保真；本台账压缩不重复产生总结文档。

2026-10-05 typed canonical边界小组repair6–9及真实sealrepair2–4多轮首次失败即停；最后原回执分别为dual completion carrier pairing conflict和draft缺幂等键，不能全归为旧fixture。主建议合法初始化单例先通、随后有限相关组有界maxfail集中收剩余失败；同Sol已采用attempt10六文件组、timeout150/ulimit4194304/maxfail10，actual exit1/25.049秒（pytest23.35秒），一次收4fail/94pass。下一轮attempt11同组actual exit0/98pass（pytest23.26秒）：三处fixture补显式model-call，golden仅恢复误迁为public的原internal输入，未改hash算法。已验证集中收错后一次复验闭合这四项；整体返工耗时仍待，不将live结果作完整固定候选验收。原raw/exit保留在A/typed-canonical-production/canonical-tests/，不把live绿项归给尚未固定的完整候选。

2026-10-05 remote正式attempt3 actual exit1/64.355秒（pytest62.70秒）：Gateway/后端/native服务与隧道已真实就绪，remote DTO仍offline。主核ROOT registry/dtos.py严格比较health.workspace_id与target.backend_workspace_id（首次在0b355的Sol WVL集成出现），remote_gateway.py两条WorkspaceTarget构造和routes/federation.py导出却漏传UUID，默认NULL；这是Sol消费者闭包遗漏，不能归旧fixture或等待。修复仍由同Sol贯穿联邦DTO/导出/投影/持久恢复/健康及真实路由，保留严格身份校验。A/root-product-verification/remote-browser-terminal-attempt-3/保留原回执和双端日志。主首次target shell读取多行Python未做shell quoting失败，第二次正确引用后成功；原失败保留，单列主协调，不归作者。

同Sol在typed真实history/fork/storage收集到v1显式importer的基线悬空：f838及live都无migration/store.py或LegacyMigrationStorage/migrate_legacy_to_v2定义，但CLI与集成仍调用。按保留的显式importer合同连接唯一现行writer，不恢复runtimev1或第二存储owner；该基线投入与typed返工分开，尚未验收。

远端UUID固定80889/18path严格导出、主机械/12Python AST/官方field7核验、只读独审无确定finding已通过，主串行集成145759f7；18path pre/postblob、guard、双祖先、共享index f2afa293不变。独审最初提出旧NULL投影恢复问题，按用户无旧开发数据兼容授权重判撤回；保非NULL现行JSON/SQLite恢复和明确缺UUID拒绝。主第一次apply --index因read-tree-only索引stat cache拒绝，未改源码；第二次分别check/cached-check后apply＋精确add完成，原commands保留，属于主机械协调。

正式remote attempt4在145759 actual exit1/69.279秒（pytest67.56秒）：联邦ready、资源create/list、attach页面和Browser grant均通过；Browser WS返回合法二进制screencast却被旧JSON-only接收helper解码，未到TerminalWS与Gateway重启。Node framing与已有integration helper分流已主核；同Sol在typed可停点窄修正式helper，主固定差异独审/集成后attempt5，不重跑无关组。原证据A/root-product-verification/remote-browser-terminal-attempt-4/。这是f838已有测试消费者遗漏，不归新增UUID实现。

作者typed阶段报告（尚未固定/验收）：全仓items/offline restore/content-part locator显式session/thread；checkpoint typed provenance/extensions与真实fullcopy fork；group semantic retry严格核extensions/source/token_usage。逐字段JSONL→canonical→JCS原样一致，原行号误判实际是companion重复统计，未改hash算法；token_usage仅group anchor。通用acceptance_metadata迁为显式extensions、lineage参数与attachment payload，全量8caller闭合，不让opaque legacy audit扩展推导身份。保留原98pass及有限失败回执，不作为完整交付。

2026-10-05远端后续：ROOT145759的六改动测试文件68pass/14.27秒，actual0/16.495秒；不与作者71相加。Browser binary helper固定b25b/1path3新增行已主strict/blob/AST/作用域Ruff0及固定差异独审，通过guard/双祖先/sharedindex后集成5fefdf82。attempt5 actual1/65.043秒（pytest63.31秒），真实BrowserWS attached/detached及TerminalWS GATEWAY_TERMINAL_OK均通过，helper修复效果已验证。首Gateway原lifespan完整关闭无残留监听；rebootGateway因本轮fresh SQLite投影引用未知连接退出3。主只读SQL确证meta.remote_gateway_connections=[]而合法UUID投影已落盘；固定树独审定位owner-scoped保存把旧空连接列表覆盖新输入。这是新写/恢复缺陷，不能清库或补默认；同Sol修唯一事务owner，来源基线尚待独审核，不先归模型。原raw/exit和identity-only事实在A/root-product-verification/remote-browser-terminal-attempt-5/。

attempt5初次端口预检尚未启动测试，发现attempt4孤儿SSH2323641占22164；主核时间/专用key/known-hosts/forward精确归属后仅SIGTERM该pid，6原日志保留。该残留原因尚未确定，不按端口批杀，不冒称为新增作者缺陷；固定树只读生命周期审查继续。源码正名计划已提交81a08457，术语预备清单146path/1246行（8generated），排除historical handoff/archive，两个bun.exe二进制匹配只记录、不改。

只读独审进一步确认 fresh连接元数据覆盖源于 ecf7e62（2026-09-22），属于继承源码缺陷，非本轮Sol。修复仍由唯一Sol闭合；attempt4孤儿的源码因果尚未证实，不将猜测当交付缺陷。

固定树独审另确认正式fixture的继承清理风险：terminate_process10秒后静默kill Gateway父进程，产品lifespan先await多个stop/aclose，再关闭独立SSH组及Browser最长120秒owner；外部预算与真实关闭链不匹配。attempt4孤儿的具体因果未证，attempt5正常close无遗留；同Sol后续定点修harness有界关闭与透明失败/精确handle回收，不按端口批杀、不改受控handoff语义。报告A/remote-cleanup-review/attempt-1/report.md，尚未修复验收。

2026-10-05 fresh registry修复：固定baseline80889→tree8cf679a，7path标准包SHA256 b647eaaf9e7aba42a75f940cd39721cd9260691adff134b9cefca65ab7e1dcb9；主严格cached重建、全部pre/postblob与hash、7Python AST、固定差异独审和作用域Ruff通过后集成8989dbb255acfcc08f3a843ad897185fb781f20c，guard/双祖先/sharedindex f2afa293均通过。首次真实SQLite组82pass/3fail保留，发现reader漏恢复connection的cursor/state/restart/candidate；第二轮85pass（pytest16.89秒，actual0/18.512秒），再加严格非法类型/负cursor反证后受影响组10pass（pytest2.17秒，actual0/3.700秒）。结果来自作者live，不与ROOT产品结果相加；WorkspaceTarget同名cursor未改。正式attempt6正在8989运行，尚不算产品验收。A/remote-registry-connection-publication/保留全部raw/exit/manifest和root-integration/attempt-1/receipt；127其他源码逐path deferred，typed仍由同Sol继续。主补业务源码0。

正式remote attempt6：ROOT8989 actual1/103.204秒（pytest101.54秒）；SQLite恢复与Gateway重启就绪已通过，随后新restarted_client GET资源收到401 user_session_required。主固定读f838与现行cookie owner，确认既有正式测试只在首次client获取guest、重启新client漏获取；local token不能代替用户身份，不放宽生产认证。同Sol在typed可停点补唯一正式test重启登录，原资源200/id断言保留，待attempt7。A/root-product-verification/remote-browser-terminal-attempt-6/保留原raw/exit/facts与10原日志；结束时精确测试端口无残留监听。该继承consumer遗漏与metadata修复效果分开计量，不称整项通过。

正式remote attempt7：guest consumer固定b25b→treee05c/唯一+1行，主strict/blob/preimage/AST/Ruff/固定差异独审/guard/双祖先/sharedindex不变后集成1de54734cab6047369a0d9ea1513dd9e432345ab。ROOT1de547正式实际0/74.575秒（pytest1pass/72.89秒），真实联邦ready、Browser附加/分离、Terminal命令输出、Gateway关闭/重启和fresh SQLite恢复后资源访问全部通过。target前后status0；精确测试监听无残留、ROOT无.boxteam，10份原日志保留。A/root-product-verification/remote-browser-terminal-attempt-7/acceptance.json；不再重跑这些未变绿项，也不据此勾typed/R/activation/delegate/federated资源URI或通用强杀harness安全。修订技能的事务/完整重开/重启认证规则已获得此次正式效果证据。

typed阶段报告：唯一Sol修复structured tool_result.result_id在唯一收集阶段映射，remap_finalize从已校验message locator取anchor sequence，移除可空projection_message_id推导。有限fork8pass（pytest30.23秒，actual0/34.476秒），覆盖fullcopy→删source→二次fork及omitted四形态重开/损坏反证；未改hash/compare。其余81pass/5fail组消费者复验、opaque成员extensions/derived_from的checkpoint重开/retry拒绝、indexed raw tool-call跨modelscope和v1显式importer闭合仍待；无完整typed固定包，不作验收。
