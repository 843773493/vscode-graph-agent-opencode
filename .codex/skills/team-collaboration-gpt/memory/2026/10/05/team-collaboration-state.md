# 团队协作实时状态与实测改进

主代理独占维护；稳定规则见 [共同流程](team-collaboration-workflow.md)。更新时间：2026-10-05 13:04（北京时间）。goal **active、无预算上限**，交接及七个 change 的实施、独审、验证、集成、清理与技能实测尚未完成。已关闭调查留在 Git 历史与对应报告，不在恢复时重复泛读。

## 当前授权与唯一实现者

用户授权架构熵减、唯一 owner、显式身份、移除旧兼容；开发生成的新旧中间数据可删除，不再询问。保普通源码、未集成独有改动与来源不明文件，不删数据掩盖 fresh writer 缺陷。当前用户要求由 **一个 `gpt-6.1-sol / max`** subagent 贯穿实现；Luna 实现者均停写，只读独审保留，不为旧人数模板增派实现者、不擅换模型。Sol 实际 `turn_context` 已核。

- 物理 ROOT：`/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。
- TEMP：`ROOT/out/tests/temp/2026/10/04/024121-team-execution`；WT：`ROOT/out/worktrees/2026/10/04/024121-team-execution`。以下证据相对 TEMP。
- Sol 唯一运行源码根：`WT/thread_owner_implementer`。此前已通过机械与窄独审的完整源码是 **`36e084a8d28fdaaa5bcc66ead915e70c409fe521`**；WVL 首完整固定树 **`d164cf6d4a0159d6cf361a56871a0e13ae42ac5e`** 已机械核验，但因 catalog 生命周期与真实 Web Terminal 创建缺口未验收。catalog 最终返修完整树 **`29513d302e12f2482738eaecab658aa189a3551f`** 的27路径机械核验与最终增量独审已通过，原P2闭合且未确认新finding；同一 Sol 已进入 Terminal 完整纵向闭包。完整9fbb已串行集成ROOT；真实全组启动因CLI消费者合同遗漏失败，先由同Sol窄修launcher，Terminal浏览器验收仍待。固定包与在途 live 分开；WT HEAD 不代表两者，不能覆盖或重同步唯一 WT。状态 index：`TEMP/coordinator/git/sol-trial-status.idx`；产物：`TEMP/coordinator/artifacts/sol-efficiency-trial/`。
- WT HEAD `ad093e0cdee2ca1225b1b9086db5e1d2c6b6b316`；**试验最初接手 preimage `e8141e9cd9677a6e3d45907526444d26baa59440`**；当前Terminal完整preimage为`29513d302e12f2482738eaecab658aa189a3551f`。不是中央 e16 或 WT HEAD。每 shell 绝对 index/workdir/编辑路径，固定对象检索，索引隔离不隔离 live。
- 主唯一串行提交者，index `TEMP/coordinator/git/integration.idx`；fresh read-tree → 精确 stage → guard record → 无 pathspec commit → verify → 双祖先。禁 shared index、amend/reset/rebase/push。测试必须 matrix 或进程外 timeout+4GB，uv/bun；代码静态，Web 改后 build。raw 按 candidate/attempt 保真，不覆盖失败、不补造、不为补日志复跑已绿。
- 三保护路径：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`。不新建整树副本/archive/.venv，不 `/tmp`，正式 workspace 镜像测试路径、fixture 源只读。

已裁定：`assembly_ref=ResourceIdentity`；寻址拒绝码 `unsupported_view`；Gateway 保留 ID `current` 创建拒绝；config layer=逻辑来源 `runtime_override`，precedence 不变，active/pending 分开。delegate 复用 Session SQLite publication ledger 同事务，不造 Team JSON 权威。internal admission 无 Turn/accepted ingress，保真实 execution link。详见 `coordinator/artifacts/owner-decisions.json`。

## 主树集成准备

主体ROOT源码集成为 **0b3559792f5f4bb346a4ce32caef1ac816b2629f**：完整9fbb源码＋最新主协作文档，893路径严格写入，提交树1cbcda0bb31969af375f53d6e8f488527c0a6aed，preimage无漂移、三保护0、共享index不变、guard与双祖先通过。证据 sol-efficiency-trial/root-product-verification/{integration,materialization-preimages}.json。HEAD每次实核；不覆盖唯一WT。

ROOT三个独有源码preimage已逐路径核并集成：`fork_boundary.py` blob `928f17eed7f50d14f101eee67e8e9ecb9894b0d1`与`session_context_assembly_query_service.py` blob `69b99c88e6298d60ffa418c09486326a45302367`均与29513相同；未跟踪`sessionCatalogOutboxRuntime.test.ts`原blob `fd4dcc48b9856f1b5549f1a655cf78826f5eaf77`，候选现代target fixture为`21bb0fc8eada4a41b1ae0e5907eda5aeaac71241`，原对象保留，最终核preimage。证据`sol-efficiency-trial/root-wvl-preparation/attempt-2/{result,preimage-inventory}.json`及`root-source-preservation/attempt-1/result.json`。不按无引用删独有源码，也不把旧status索引观察当真实丢失。

**当前ROOT：已集成并恢复全组，Terminal真实产品链路已验收。** 主体commit0b355（893路径）后，CLI/launcher窄修两包9fbb→32219→f9ac共5路径独立核验并串行提交 **c525e3463c14e6a7597dbd69d482a49bb52162a1**。主审发现LF-only会拒Windows CRLF，同Sol两路径增量已修并定向1pass。就绪等待会及时报告退出/collect，health后再核同PID与ready；原120s失败与测试JS转义原raw保留。真实CLI、原21绿范围、单例修复、既有systemd4及CRLF分别引用，不相加成新全组绿；两包strict/blobSHA/AST/Ruff0/Bunbuild0/保护0/deferred Browser42核过。精确原始收据与主独审在 root-product-verification/launch-forward/。

当前unit boxteam-dev-cff6686cbc5b0e76.service active/running，MainPID567122，物理ROOT/home、offset16（frontend8027/Gateway8030）。实际经8027验证gateway health/workspaces/workspace均200、同一request_id；新Session mainThread绑定、UI建Terminal、iframe grant/WS与真正PTY输入输出、promote/stop后最终输出、detach/delete都过且浏览器error0。原browser-attempt2总exit1来自主误用已移除Session DELETE做末尾清理，业务断言均已完成；未重跑绿链路，只经正式navigation operation补清理，committed且pending_settlement=false、cleanup-only exit0。原browser-attempt1主误把backendID当Gateway路由ID的404也保留，均不归给作者返工；注册目标与backend稳定ID已分别核验。验收 root-product-verification/terminal-product-acceptance.json 绑定原rawSHA/exit与限定结论，不伪改失败收据。

两授权workspace生成运行数据已清，普通代码、workspace identity保留，证据 runtime-data-cleanup.json。默认workspace本轮测试Session已正式删除，无新增project-root .boxteam；主树仅协作文档待提交，Sol唯一WT继续Browser42个在途源码、不覆盖。下一完整Browser baseline为 **f9ac4a12a11910291ea58c2ac1172bcb10b1c603**；原其他typed/activation/delegate/federated及最终唯一WebE2E/OpenSpec仍未验收，goal保持active。

## 已通过切片与效率证据（不重验未变部分）

主补实现源码仍为**0**。Sol接手为2026-10-04 18:13:52.8585 UTC；fork首完整候选106.84分钟。Luna旧fork4pass17fail/33.8s是不同合同测试时长，前置与起始投入不可比，不算速度倍率。模型表现、作者返修、fixture接手和主协调成本分别记录在`sol-efficiency-trial/efficiency-comparison.json`；Terminal准确开始时间未知，`terminal_write_release_at=null`，不用catalog freeze推算耗时。

| 切片 | 已通过固定范围与证据 | 仍待 |
|---|---|---|
| fork | 主体`f25989c37a27bead6954ab78f627b0e4dfe0c513`20路径、47pass；组合`ce5950b628a86db61826a71d4d8e05d69eafeaee`真实HTTP/startup/anchor7pass；请求意图增量`4cf1d8ddf43c7c32ce27f883e8848e72c383e6e0`11路径、新增/迁移24pass，机械及增量独审通过。`fork-intent-verification/`、`fork-intent-review/attempt-1/report.md` | 已ROOT集成；sealed model-call/retry assembly及itemized9.5整项未验收 |
| Runner | `847e7bcf8fced202e47006f6fe9480b2cc2357dc`29路径，真实20pass、受影响unit37pass，机械与窄独审无finding。`runner-verification/attempt-1/result.json`、`runner-independent-review/attempt-1/report.md` | 已ROOT集成 |
| Graph/runtime | `36e084a8d28fdaaa5bcc66ead915e70c409fe521`41路径，机械outside0/AST0/保护0，真实定向与独审通过。`graph-runtime-verification/attempt-2/result.json`、`graph-runtime-independent-review/attempt-1/report.md` | 已ROOT集成；真实编译图retry随activation补证 |
| WVL | `d164cf6d4a0159d6cf361a56871a0e13ae42ac5e`作者100/完整240路径机械及38次原raw/exit核过；Web1257pass/build0/tsc0；同132path Ruff继承111、新增0、exit1。旧13Web失败、UUID/health接管及fixture已定向收口 | Terminal真实创建P1，故不是完整产品验收 |
| catalog返修 | `d164→29513`27路径；strict/blob/SHA/AST27/保护17/raw20实核。最终`9d95d9f668d734b15672d7dc0b131d5a8a828dc1→29513`静态独审通过，Goal/Job迟到writer P2闭合、无新finding。`wvl-catalog-owner-verification/attempt-2/result.json`、`wvl-catalog-owner-independent-review/attempt-2/report.md` | 已ROOT集成 |

上述路径相对`coordinator/artifacts/sol-efficiency-trial/`。不同测试范围重叠不相加；审查者只读固定对象、未替作者跑测试。catalog消费者101、navigation9、fork7、完整custom generator1pass/249.70s、真实Provider替身生成15pass、Goal shutdown8pass、双失败因果1pass、admission11pass原raw/exit已核，**不重跑生成器或旧matrix补日志**。真实失败raw保留，继承Ruff红不得说全仓绿。

## 当前切片与后续唯一队列

精确基线、任务ID、允许范围和产物只在`coordinator/artifacts/sol-efficiency-trial/implementation-queue.json`维护；恢复读当前条目，不泛读已关闭调查。粗切片曾漏原task ID，属于主协调问题，不能归给Sol/Luna。

1. **Terminal完整包及恢复增量已通过独审并主集成，真实产品已验收，Browser仍待。** 唯一产物根 `coordinator/artifacts/sol-efficiency-trial/terminal-owner-production/`。完整 `29513→61812a6f0ab32b786bd8e03b5405ee4d9a47d5d4` **118路径**；主strict重建/old-new blobSHA/AST66/保护0/旧historical符号全生产与tests引用0/17原rawSHA及exit已核，作者freeze时outside source0。`complete-delivery/{evidence-binding,primary-mechanical}.json`。原raw不重跑、不相加：owner87/API9/Web36/caller44/GatewayApi30/Node33；真实Workspace资源4、managedGateway2、transport12、tool+codec9、attach最终3及finally单例1都过。官方gen/check、Web tsc/build exit0；Ruff同63文件base/current30条、新0、exit1，不说全绿。真实tool发现Struct把0转float而缺最终回执，已typed退出码修复并核未结束缺值/0/9；Node失败attach清理和await版本撤销边界已根修。首轮fixture合同错配、缺退出码及Node失败保原raw，详情仅留效率JSON。

   原最终独审确认已停PTY崩溃lease锁死与同代丢回执重试不核Node两项P2。同Sol增量 **61812→9fbb83075656a565c27524840efe70e60f6533df，29paths**，提取唯一公共生命周期并按身份/revision/终态结清；unknown或仍running保阻断，普通list不清unknown且deleted+unknown仍可发现，已deleted重试不重放。主strict/blobSHA/AST26/5组原rawSHA与exit/保护0/两index不变/freeze外源码0全部核过，既有Luna只审新增依赖后无仍可行动finding。精确证据 browser-owner-production/terminal-shared-recovery-delivery/primary-mechanical.json 与 independent-review.md；最新owner31、caller9、真实fixture1、真实API/Gateway/tool9、crash/retry3分别过，不相加。Ruff25条同基线、新0/exit1。原真实恢复1pass2errors属测试父registry/fixture未close ActivityStore和catalog导致flock残留，同Sol修finally后3pass40.25s；不是并发测试或生产恢复返修，原失败raw保留。早审误套30min合同已撤回且未源码返工。ROOT893路径集成见上，Browser另交；主补业务源码仍0。
2. **residency稳定差异早审与实现并行。** 原草稿 `eae79` 的冷读不造slot、成功drain回收、失败保重试owner、同一tracker递增generation已静态核；预审发现listener await中接受新blocker。Sol确认真实claim writer原先先持久写再push，外层不能保证不可达；已在关闭重核blocker后无await设置closing，writer写前消费同一准入，shutdown显式区别普通unload。稳定 `29513→9e7013e25d87542d5bfcef7f7da088c7986c12fc` 三路径 strict/blob/SHA/AST及两sharedindex主核通过，**增量独审已通过，原blocker/unload边界闭合、无确认新finding**。owner87包括1000冷读/100次unload、失败重试/旧代回调及真实NodeDebugSessionStore/claim writer屏障拒绝且无落盘。主指出测试屏障finally释放，Sol同WT窄修，`residency-guard/attempt-1` **1pass/0.80s/exit0**已核，未重跑87；完整包将含新测试blob，生产两blob仍9e。报告与机械证据统一 `residency-stable-pre-review/`，`final-report.md` SHA `45b5ace547c906872659d504e600be679fd340f8270ceda0e8a4ecc4b4140cce`。shutdown的durable-only claim及外部在途HTTP排空未由此窄审背书。Terminal实际入口定向已过、ROOT Terminal浏览器已验收；整体goal产品范围仍待；不夸成跨tracker generation唯一，不给模型速度倍率。
3. **typed canonical与producer。** 原itemized1.5/1.6、context4.6/6.7/7.1：metadata→opaque namespaced/versioned extensions，typed ProjectionGroup/ItemProjectionIdentity/ToolResultEvidence/ItemSourceManifest/source binding贯穿writer/SQLite/JSONL/checkpoint/fork/history/provider；R01–R07真实producer及条件ToolSet roots接线。已有selection_role/replacement_policy/source_ordinal、RuntimeNoticePayload保留不重做；不只调用helper求勾、不做旧数据兼容。精确静态证据同上一项，原task清单`fixed-candidate-task-inventory-29513.json`。
4. **activation与delegate。** activation底座已有7路径draft，上层需唯一registry、持久workspace key/freshmarker、execution/model-call冻结/恢复、原子sealbind，无None/当前源旁路；真实编译graph reasoning-only→有效正文及耗尽retry、逐model-call/canonical身份和唯一终态仍待验证，复用Runner/runtime证据。sessionVRN5.3的activation_bindings.display_uri/context_source_control_states持久新写/read-path仍未验收，不能由helper/schema推断完成。`coordinator/artifacts/activation-production-wiring/review-plan.md`。delegate复用Session SQLite publication ledger与child publication同事务，贯穿recovery/factory/startcaller，published不倒删。
5. **远端VRN/target-main。** 固定29513 grammar无authority，远端scope在资源访问前unknown_scope；联邦port仍unavailable。主已独立核并纠正报告：`app/api/sessions.py`已有GET `/{session_id}/main-thread`与SessionMainThreadDTO，后续只接通WorkspaceSessionMainPort消费已有路由，不新造endpoint。复用channel/grant/policy/credential和本地context/cursor，唯一authority解析；真实B⇄A⇄C、同本机authority等价、一次transit/两hop/deadline、最新权限、结构化拒绝/零副作用/无locator。更正已通知同Sol；`remaining-addressing-contract-review/attempt-1/report.md`已完成，仅静态、未测试/服务。
6. **最终产品验收。** 公共Schema依变更官方生成、单跑tsc，build不代类型检查；核实际tag/reserved而非照抄Pending19/20早期编号，不手改pb2。按原task ID与实际入口对应证据，strict OpenSpec及既有唯一Web E2E `tests/e2e/clients/web/test_basic_chat_tool_loop.py`增加持久Browser/Terminal、unload/跨Thread关联/来源Session删除/错误反馈，真实浏览器验收后清理。本goal七change全完成前保持active，不将未勾任务自动当缺源码，也不据组件绿勾整项。

## 失败经验与原始证据

WVL裸read-tree损共享index，事故前无精确hash备份，按历史tree重建不能称逐blob精确恢复；作者已停写、收窄权限。`coordinator/artifacts/shared-index-incident/attempt-1/`。Sol释放窗口ROOT共享index基线为`d541fd806cf0f78241ab421632efa325d4a84805bcb8c5584b3faf14b582ea2d`，WT默认index为`e1cb50fdd86644b869eaa547caf1c572413c768fbb838e1cd636ea248670e73d`，路径用absolute-git-dir/index核，不能用受私有env影响的--git-path index。

主曾误把私有status索引当共享索引并恢复错目标，约九分钟单列；实际ROOT共享index始终不变，源码/refs未改。`sol-efficiency-trial/coordinator-resume-preflight/attempt-2/correction.json`明确撤旧误报。当前寻址审查又因陈旧port注释误报已有endpoint缺失、把helper推成activation5.3闭合；主核固定路由后同reviewer窄更正，在派单前阻止重建，没有确认实现返工，不臆算耗时；`efficiency-comparison.json`及`scope-review-correction-git.json`。

目录迁移已释放系统盘约238GB；根terminal-manager、授权旧.boxteam与可复现副本已清，独有源码保。目录审查`docs/handoff/20261003-151500-team-collaboration-directory-review.md`，原服务环境`out/tests/temp/2026/10/03/160552-workspace-migration/coordinator/artifacts/migration-state.json`。不复用迁移排除的历史工作区、不再复制整树；切换时只清上述已明确授权的开发runtime。
