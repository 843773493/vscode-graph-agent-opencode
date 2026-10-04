# 团队协作实时状态与实测改进

两个团队共用、主代理独占维护。恢复/派单/集成/报错时读本文件；稳定规则只在 [共同流程](team-collaboration-workflow.md) 维护。更新时间：2026-10-04 22:36（北京时间）。goal **active、无预算上限**，交接及七个 change 的必要实施、独审、验证、清理和技能实测未完成。

## 授权与路径

用户已授权架构熵减、唯一 owner、显式身份、移除旧兼容；开发生成的新旧中间数据可删除，不再询问。保留普通源码、未集成独有改动和来源不明业务文件；不能删数据掩盖 fresh writer 缺陷。分工建议不是模板，按实际返工、入口遗漏、等待和中央补救成本选择。

- 物理 ROOT `/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。
- TEMP=`ROOT/out/tests/temp/2026/10/04/024121-team-execution`；WT=`ROOT/out/worktrees/2026/10/04/024121-team-execution`。以下证据路径相对 TEMP，每次派单给**字面绝对** workdir/index/artifact/report，不能自行简化日期或落 worktree/out、/tmp。
- GPT subagent 精确 `gpt-6-luna / max`、不带前缀，创建参数/实际模型已核，证据 `coordinator/artifacts/{model-context-latest.json,u05-model-context.json,new-review-model-context.json}`。用户选择覆盖旧 AGENTS DeepSeek模型要求。至少3、最多15常驻，主统一派单，agent不自行派生；429保源、不密集恢复、不换模型。
- 每shell全程 export 绝对 `GIT_INDEX_FILE`、`GIT_OPTIONAL_LOCKS=0`，显式物理workdir、绝对编辑目标。固定检索 `git grep <tree>`/`--cached`，差异给base/target；独立index不隔离默认live检索。新派单主实际read-tree初始化索引。
- 主唯一串行提交者，index `coordinator/git/integration.idx`；fresh read-tree→精确add→record→commit无pathspec→verify→祖先。禁amend/reset/rebase/push/defaultindex写。
- 测试走matrix或 `timeout N bash -c 'ulimit -d 4194304; exec "$@"' bash <命令>`；uv/bun，代码静态、Web改后build。raw按candidate/attempt保存真实退出码，不覆失败、不为补日志复跑绿。正式测试workspace镜像out/tests/testpath；fixture源只读，临时仅分配根，不注册源码根/复制全树。
- 三保护不动：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`。

裁定：`assembly_ref=ResourceIdentity`；统一寻址owner拒绝码`unsupported_view`；Gateway ID `current`创建拒绝；配置layer表示逻辑来源，sqlite读标签→runtime_override、precedence不变、active/pending分开。delegate复用Session SQLite collaboration/creation ledger和publication同事务，不造Team JSON权威。internal admission无Turn/accepted ingress，保trace/canonical/真实结果及显式execution link。证据`coordinator/artifacts/owner-decisions.json`。

## 主树与验收事实

主HEAD **`19a487da08bc78aef28747ac8c141226c053dd54`**，最新三份技能/记忆文档；quick_validate、13链接、guard/祖先/defaultindex/未知fork保护全0，证据`coordinator/artifacts/skill-current-entry-validation/result.json`。本次续调尚未集成源码；本文与SKILL当前dirty由主维护。

上一主源码 **`7c39f2d10c7f6a97b4ef50dd6fe7b7e8c0e6a732`** = O02a完整24paths，主28queue+2lifespan、R43及追加test-cache隔离独审通过，证据`coordinator/artifacts/o02a-main-integration/commit-result.json`。此前O02b/route、L01、M01/M02、UUID、配置来源/地址等已集成绿复用；历史关闭事实见Git与原报告，不重新全矩阵。

主树唯一未知源码diff：`app/services/infrastructure/rollout_context/checkpoint/boundary/fork_boundary.py`，blob **`928f17eed7f50d14f101eee67e8e9ecb9894b0d1`**，保、不覆盖。默认 `.git/index`极陈旧未动，SHA256 **`53bff61fe3f4c69658bd178ce4e59eb441b654a0764ed0e211f0a14f34f71954`**，裸status不能判断删除/变化。

交接 [bug hunt](../../../../../../../docs/handoff/20261003-060700-bug-hunt-and-redundancy-handoff.md)。实际OpenSpec勾选仍 itemized52/87、uuid49/49、unified5/40、sessionVRN20/35、multiworkspace5/34、context21/57、persistent2/21；raw `coordinator/artifacts/current-openspec-progress/`。不因部分gate/候选可合而勾大任务。ThreadRuntime/delegate/activation/P05及真实产品验收仍必要。

## 当前组合：全部仅对象，未主源码验收

| 对象/增量 | 当前事实与证据 |
|---|---|
| 完整E01/E02/E04基线 `73c053cb9c8612ed40e48ad35b05bcac88611756` | E04165path/321PythonAST0；保main M01/M02等前置，不能旧全union替HEAD。`coordinator/artifacts/e04-full-current-union/` |
| E01low14+E04R47/R49+U03backend16+主docs → `293740a4003370f05440f339d0c275bcc3f1d956` | 307path逐preimage已同步唯一E01 WT（HEAD仍ad093）；158PythonAST0，保作者在途。`coordinator/artifacts/e01-current-dependency-sync/` |
| U03Web22 `abe7→89529419b680af323b5f66eb1da563c50a0bddc0` | strict/blob/hash0，SHAa46b7075...；合293740成4b619。`coordinator/artifacts/u03-web-central-mechanical/` |
| R52 SQL ordinal三path `abe7→bc78a6ef204e267d172ba92c846b9f0362c6aa3b` | storageentry必填、SQL SELECT membership ordinal、fixture真实ordinal；AST/Ruff/独审0，三path同步E01；合为 **`dd6ef4419d64af13c2077014aa8cac81b54ccc9a`**。`coordinator/artifacts/r52-central-merge-and-sync/` |
| 旧六publicgen+主19a docs → `c8656dbd89ed8fe71225965bae4a72f3f31dd03f` | 旧268f生成已被R53新proto取代，不能用作最终合同。`coordinator/artifacts/current-docs-union/` |
| O03Web original5418→1a795完整30paths，c865合→ **`293a34d108b8df39bf524aa33a787cc53be00525`** | current已store两同blob，真实增量28；两driver解后与1a795同blob，无新增审查diff。strict/hash0，patch037a4cfa...。`coordinator/artifacts/o03-web-resolved-central/` |
| R53 dd6ef→ **`5c5096a47a70d76f1ed5e99e10a9962f39ecf4a8`**20paths（19M1A） | 补组件/异常/include/integrity/zeroTurn scope，公开execution去顶层ordinal、proto reserve10/name，storageentry ordinal保。standardpatch28f49060...，strict/blob/AST0；合293a→ea7ad6。`coordinator/artifacts/r53-current-central/` |
| R53新六publicgen → **`b295516f25ec31bab8237684302a74ba01f7ebe4`** | 绝对buf lint/build/generate0、六预期changes、PythonAST0。首裸buf PATH127保attempt1，修后attempt2。U0340path同步唯一E01 WT：38写/2target，全preimage/postimage0、不改HEAD/fork/T05。`coordinator/artifacts/{r53-proto-generation,r53-dependency-sync}/` |
| T05a dd6ef→ **`3b843f387d2916c962ce1b7abb46e10f621c232a`**2paths | thread_residency/newtest，仅runtime owner单元切片；中央strict/hash/AST0、patch032676bd...，合b295→ **`b79533c1fcebc252066b9697415c1803935c5365`**。独审P2 state投影待forward。`coordinator/artifacts/t05a-central/` |
| WVL 73c→ **`f6e66b56dd56f502a88079ca2fdb4e48fe1d9f2a`**162paths | strict0/93PythonAST0；首1f4 main语法错误拒收。作者shortindex227b...、中央fullindexa3b952...都重建，不能跨格式比字节hash。14readydeps均在且更新；合b795仅5Web冲突，原合流者正对象解决并补requesttarget seam。`coordinator/artifacts/wvl-current-central/attempt-2/` |

## 当前任务、依赖与验证

### E01 R51小forward → E01A（thread_owner_implementer）

唯一源码WT=`WT/thread_owner_implementer`；全shell index=`TEMP/thread_owner_implementer/git/r51-forward.idx`；报告/产物=`TEMP/thread_owner_implementer/artifacts/r51-current-forward/`。index含在途不要重建丢stage。三保护外完整必要caller/fixture/tests已一次授权，不routine停问。

R51原scope/GlobalEntityRef finding已推进；recovery统一(session,thread,ns)、MessageService.list真实scope（保WVL VRN/context）、AppendWriter session参数、internal checkpoint_reminder nullable turn、fullcopy owner表/relations迁移纳完整闭包。fullcopy非终态含interrupted无final规范要求cancelled、reason=fork_source_runtime_not_copied、resume/dispatch_replay拒turn_not_resumable；新turn replay另链不能把copied运行复活。sourceJob真实GlobalEntityRef不造targetJob。

最后fullcopy acceptance生产target admission key确定性独立、唯一codec按target preimage重算hash，sourcekey/hash/job留immutable lineage，r6定点绿/Ruff0。全fork r7=4pass17fail，source resolver/target aput/cleanup owner scope缺，raw保。作者正真实service三个边界分别catalog canonical scope（不全程source包target）；scope后target aput缺admission：旧顺序先普通message运行append、fork最终transaction才事实materialization。需在现有fork owner统一target事实准备→journal保护复制canonical/checkpoint→finalize，复用codec/store，不造userTurn/Job/假admission、不删运行guard或平行writer。尚无finalfixed。

小forward固定后E01A：JobService唯一user identity producer，独立job/turn/key；generation现有唯一ledger reserved全binding重派复用，protected generation/service.py不改。Job/runtime/pending/DTO/proto必填可空turn，internal None，不TurnStream/NoOp/fakeTurn；ChangesRecorder/FileEdit nullable保真实job/execution结果；startup从精确owner持久admission解析。tool inspection仍import已删NodeDebug MAIN_THREAD_ID，阻production import；E01A/T05b去纯工具DTO假durable owner耦合、不复活alias。计划`thread_owner_implementer/artifacts/e01a-producer-plan.md`。

### U03 R53/bootstrap（architecture_reviewer实施，fixed_delivery_reviewer独审）

R52真实SQLite reverseorder+zero ordinal两case2pass2.21s，证据`coordinator/artifacts/r52-runtime-sqlite/attempt-1/`，依赖E01WT recovery；裸abe7不能归该绿。fake unit最初7pass2fail，R53只复验失败+3新exception/defaultinclude=5pass1.77s。Webtimeline29pass，缺依赖链接导致3files collectionfail后只复验3files=15pass1fail，合44pass。internal-only冷bootstrap跳过tail→[]；tsc另揭defaultViewChanges.test仍顶层ordinal。Webbuild0/16.93s，tsc2（其它catalog必填为O03Web未同步前置，不归U03）；所有raw `coordinator/artifacts/r53-runtime-{python,web},r53-web-{types,build}/attempt-*`。

独审两P2同中央证据，其余所审无newfinding：`coordinator/artifacts/r53-forward-review/report.md`。原作者只ROOT对象/精选快照窄修bootstrap/page/对应fixture，index=`TEMP/coordinator/git/u03-bootstrap-forward.idx`已read-treeb295，产物=`TEMP/architecture_reviewer/artifacts/u03-current-merge/r53-bootstrap-forward/`；不写E01WT，中央后sync。保44绿只重验failed及新diff，最终组合tsc验完整O03依赖。

### T05a stateforward → T05b生产（behavior_reviewer实施，architecture_reviewer独审）

唯一E01WT中独占residency/runtime/tests，不runner/job/factory/main/container。2path固定8unit绿/static0复用；独审P2 snapshot缺loading/unloading，依据itemizedspec284。报告唯一`coordinator/artifacts/t05a-runtime-review/report.md`（误放作者fixed副本已按字节搬回/删、SHA5ee8447c...）。pair/singleflight/pendinglease/cancel/fence/closefail-retry暂无其它finding。

原作者stateforward index=`TEMP/coordinator/git/t05a-state-forward.idx`已read-tree3b843，产物=`TEMP/coordinator/artifacts/t05a-runtime-owner/state-forward/`，按真实build/unload task投影不第二状态权威；主另核所有waiter取消后的builder/close后台失败不能只task.exception()吞、需透明可发现。新增对应必要unit/static后fixed，再独审增量。

T05b仍必要：Runner真实admission校验之后lease、先持久GraphBinding resolve再lazycompile、graph per-generation mutable调用输入不捕获、reactor归Thread scope、production sweep lifespan与正确shutdown顺序；依赖E01A，不把T05a unit称8.14完成。计划`coordinator/artifacts/thread-runtime-current-plan.md`。

### O03 backend + Web R54（session_vrn_convergence / terminal_owner_api_implementer）

共享WT=`WT/navigation_authenticated_scope`，backend/Web独占。backend index=`TEMP/coordinator/git/o03-backend-final-owner.idx`已read-tree293a；产物=`TEMP/navigation_authenticated_scope/artifacts/o03-backend-current-fixed/`。旧f62213paths30pytest、R50trigger9df2paths7case/独审无finding复用。

新freshv5唯一nodes.title_source，folderNULL/sessiondefault|user|auto，creationjournal freeze/publish同txn，manifest剥离/getlist从node；publicUpdate仅agent/provider、Create去公开title_source，readonlyDTO保；rename只现有name+CAS intent、不新增第二writer；typed-auto唯一queue defaultsource/name/revisionCAS，失配not-applicable、真实receipt后权威get/STATUS_CHANGE。5同步HTTP/facade/publicDELETE下线，internalfork cleanup同topology/drain原语防queue自死锁。E2E最后directPATCHtitle已迁queue。

作者105unit+7shortwrite/delete+3queue/lifespan **仅工具转录**，未找到raw，标lost不补造/为日志重跑。首次v2fixture/DTOclassheader误删/include_self漏rootadmission/缩进失败保可追溯证据。统一`fresh_catalog=True` helper仅删复制品DB/WAL/SHM不改fixture源。fixed须保base NodeDebug/gate用例（live删除片段不被接收），故新增合流实际差异主后定向验。作者曾未freeze结束已恢复，仅机械freeze/caller完整审计，不等db2。

Web原30/current28fixed中央0，但R54 P1旧principal在当前认证下reconcile/dispatch，P2delete丢waitForTerminal receipt与Explorerprune竞态，P2恢复拒绝可能不可见。原作者先R54forward后P05；index=`TEMP/coordinator/git/o03-r54-web-owner.idx`已read-tree293a，产物=`TEMP/navigation_authenticated_scope/artifacts/o03-r54-web-forward/`。fence在途/未来旧身份，不只timer；权威receipt直接用，恢复拒绝稳定visible。只新/改tests+tsc/build，原103/21绿不归新forward。报告`coordinator/artifacts/o03-web-fixed-review/report.md`。

### W/V/L02合流（integration_fixture_implementer；fixed_delivery_reviewer独审）

只ROOT对象/index/产物，禁live/ref/commit。actualcommon=d76、current73c、resolvedsource **db2a24b6a1e60fdc8c1890f381fc74f5bab9590e**，不能用原source504b；VRead3887→9aff2、L02caea29→9eae2、W04仅handproto place_under_source，六gen保current，排.codex，三保护保。14ready原清单`coordinator/artifacts/l02b-ready-dependency-tree.json`、完整旧resolved`l02b-resolved-main-preview.json`（staticfixfb1e），actualnecessaryunion不用数字门槛。

d76→current462/fixed194/intersection15，排2.codex/1同blob=12shared；旧26/175/172不适用已撤。实际union175起点→162diff，strict0但首main语法錯→f6e93AST0；短index/fullindex字节hash差异中央第一次误判已纠正。14ready均存在且更新，无需L39。f6e合最新b795仅5Web冲突：api/session/sessionCatalog.ts、hooks.tsx、lifecycle、explorer、shell/catalogactions。

作者复制中央stage到独立index，只对象保O03新outbox/旧writerDELETE移除与WVL身份/VRN。实际新增依赖：catalogenqueue/status/snapshot API转 **GatewayWorkspaceRequestTarget**，currentO03driver/runtime还是route字符串；对象合流者唯一补target seam穿透（routeID与backendUUID分开），R54作者只原合同语义forward，不写同live；后中央按真实base符号合两owner。artifact=`TEMP/integration_fixture_implementer/artifacts/wvl-current-owner/current-resolved/`；中央conflict index=`TEMP/coordinator/git/wvl-central-current.idx`（不能其它任务重建）。WVL独审index=`TEMP/coordinator/git/wvl-integration-review.idx`read-treef6e，报告=`TEMP/coordinator/artifacts/wvl-integration-review/`，只公共shared新增差异及必要caller，不全162重复审。

### 后续P05/activation/delegate

P05a Terminal已派原Web作者，先闭R54；index=`TEMP/coordinator/git/p05-terminal-owner.idx`read-tree293a，WT同navigation，产物`TEMP/navigation_authenticated_scope/artifacts/p05-terminal-owner/`。TerminalManager/StateStore真实领域owner，扩sourcepair/retention/association/revision/unattached/owner原子操作，先Terminal纵向后Browser/API/UI。无新ResourceManager/registry，不复制lease事实，外部stop/delete必须owner核实，未知可发现；旧开发状态不迁/双读。mountedresolver依赖归W/V，**不归T05**；P05报告旧命名待修，不能按误名造registry。报告`coordinator/artifacts/persistent-resource-current-plan.md`。

ResourceActivation已有9.1/9.2 domain/SQLite，9.3 production coordinator只有测试引用，seal/preparation调seal_context_plan未传activation；先接真实owner输入、hash/manifest/pair，不新模型/registry或把snapshot当Terminal实体。9.5materialization需8.11不塞首slice。behavior只读规划index=`TEMP/coordinator/git/resource-activation-plan.idx`read-treeb295，报告=`TEMP/coordinator/artifacts/resource-activation-current-plan/report.md`，其T05forward优先，规划尚未完整。

delegate发布前准备复用唯一Session SQLite ledger与childpublication同事务；publishedchild不倒删，真实recovery/factory/startcaller仍必要。E01A/T05b/8.11及activation/P05/真实唯一WebE2E均不能省略。

## 分工实测与调整

| 证据 | 当前调整/效果，未证点 |
|---|---|
| E02残hunk、E01low60c漏生产、V02漏Web/删除 | 固定tree标准patch/strict/blob/outside机械门能拒残包；不证明真实合同，U03静态完整仍反复漏入口 |
| 多公共接口/依赖树失配，主旧union回退新owner | 唯一提供者先固定必要callee/签名、actual共同祖先/当前main前置；按符号保两owner，旧数量门槛撤，WVL已构162但语义新增仍审 |
| U03同作者贯穿却SQL/exception/component/bootstrap/fixture连续漏 | 一次授权必要消费者闭包、真实最小入口先验，机械+静态+独审；纵向尚未显著减少返工，不能宣称最优 |
| E01fork每个guard后仍问fixture/tests范围，旧append与materialization顺序红 | 沿既有纵链授权继续，不routine停问；需核心owner统一顺序，不丢guard/造事实；E01A仍未端到端验 |
| R46 HTTP绿UI旧writer；R54 principal/receipt/rejection缺真实竞态 | 沿用户动作→权威owner和身份代际/回执消费审；原作者小forward、独审只新diff，原绿按candidate依赖复用 |
| O03恢复错cwd、审查wrongroot、报告写作者fixed，日志raw遗失重复 | 派单字面绝对workdir/index/编辑/产物；主核实际文件不据status；报告字节搬回，lost如实；重复失败仍观察，不能称规则解决 |
| T05a8case绿仍缺状态闭集；WVLpatchstrict但mainsyntax錯 | 完整变更静态与contract真实入口先门，state投影复用task事实；未生产接线不冒充整体绿 |

比较只在相近范围记录首次可重建性、入口遗漏、返修、公共合同等待与中央修补量。独立owner适合单人纵向，公共接口先固定消费者顺序；目前证据不足以全面改变分工。原作者修必要caller/fixture、机械导出、复用已过审查已有局部收益，实际新缺陷仍需验证。

## 目录、服务、清理与下一步

系统盘旧副本已删，迁移释放约238GB；14可重建副本删、67项分类、23092保留文件校验。[目录审查](../../../../../../../docs/handoff/20261003-151500-team-collaboration-directory-review.md)。默认/Drive旧.boxteam授权清空，根terminal-manager与空.boxteam删，创建者未证。E01旧snapshot2-base约261MB已核3428baseline/32独有delta保tree1e312a7e+262120bytepatch严格重建后删，证据`coordinator/artifacts/e01-snapshot2-cleanup-{audit,result}.json`。在途worktree/独有源保，不再复制整树。

服务default/Drive ready、前端8027初始化三API200/request_id一致为历史。恢复用 [migration-state.json](../../../../../../../out/tests/temp/2026/10/03/160552-workspace-migration/coordinator/artifacts/migration-state.json) 原环境：BOXTEAM_PROJECT_ROOT旧入口、BOXTEAM_HOME旧入口/out/development-runtime/boxteam-home、BOXTEAM_DEV_PORT_OFFSET=16；cwd数据盘。先dev:status，不另一unit/另起整组。

下一步：收R53bootstrap/T05state/O03backend/R54/WVL5+targetseam固定包→strict/真实caller/增量独审→仅受影响失败与新入口验→先串行主集成可独立通过owner，再E01A/T05b/delegate/activation/P05；最终组合协议/OpenAPI、OpenSpec、唯一WebE2E/真实浏览器、产物清理。未全部完成保持goal active，不只改文档收工。已关闭细节在原报告及Git历史（19a版state含完整事故链），不每次恢复重读旧调查。
