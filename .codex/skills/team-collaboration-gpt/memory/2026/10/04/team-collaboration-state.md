# 团队协作实时状态与实测改进

两个团队入口共用，主代理独占写入。恢复、派单、集成及报错时读本文件；稳定规则只在 [共同流程](team-collaboration-workflow.md) 维护。更新时间：2026-10-04 16:46（北京时间）。goal active、无预算上限，交接及七个 change 的必要实现、独审、验证、清理和技能实测仍未全部完成。

## 当前授权与现场

- 用户授权按架构熵减自主统一 owner、显式身份和单链路，删除旧兼容；源码开发生成的新旧中间数据可直接删除，不再询问。保普通源码、未集成独有改动、来源不明业务文件；不能用删数据掩盖 fresh writer 缺陷。
- GPT 团队精确 `gpt-6-luna / max`，不带前缀；主模型由用户选。至少3、最多15常驻，主代理统一派单/审查/集成。既有九个 agent 的创建参数与实际模型均已核，证据 `coordinator/artifacts/model-context-latest.json`、`u05-model-context.json`。429保源和真实错误，不换模型、不密集恢复。
- 物理根 `/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。活动 worktree 根 `out/worktrees/2026/10/04/024121-team-execution/`，产物/索引根 `out/tests/temp/2026/10/04/024121-team-execution/`。下文路径相对此物理根；每次派单仍给字面绝对 workdir/index/artifact/report。
- 当前主树 HEAD `80cd707a6f5bc8dbecc4826028ac946c0f17e6e4`；本笔仅技能/流程/状态三文档，quick_validate、链接、diff-check、guard及祖先通过，证据 `coordinator/artifacts/r33-practice-main-commit.json`。D01 helper/流程五文件d76和技能实测记录两文件400已提交。L01主树17pass5.78s/Ruff0，M01/M02主树40pass及独审证据复用。恢复时用 fresh 独立索引核真实状态；共享 `.git/index` 极陈旧，裸 status 的 D/MM 不能当源码差异。本文件与流程由主代理独占。
- 主代理唯一串行提交者，索引 `coordinator/git/integration.idx`；fresh read-tree→精确add→record→commit无pathspec→verify→祖先。禁amend/reset/rebase/push与共享index写入。
- 三处受保护路径：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`，不能修改。
- 测试/probe用matrix或外部 `timeout N bash -c 'ulimit -d 4194304; exec "$@"' bash ...`。uv/bun；保存raw stdout/stderr及实际exit。已pass不为补日志重跑；缺失raw如实lost/转录。源码改后静态，Web改后build。
- 已裁定：`assembly_ref=ResourceIdentity`；统一owner拒绝码`unsupported_view`；Gateway ID `current`创建拒绝；配置layer表示逻辑来源，sqlite读标签改runtime_override、优先级不变、快照独立。delegate复用Session SQLite collaboration ledger与publication同事务，不另造Team JSON权威。internal admission无Turn/accepted ingress，semantic输出保原，execution link显式。证据 `coordinator/artifacts/owner-decisions.json`。

## 集成事实与范围

交接：[bug hunt](../../../../../../../docs/handoff/20261003-060700-bug-hunt-and-redundancy-handoff.md)。已集成T01、G01/G02、S01/S02、U01/U02、T04、G03、配置来源/C03、S03/S04、UUID自然allocation/U06/U07、B01/B02/J01、S06-v2、M01/M02；闭合历史以Git与对应报告为准，不复跑完整绿灯矩阵。近期提交：UUID真实回拨`b75dbd90`、B01/B02/J01`28359675`、S06-v2`5383baf3`、稳定流程分离`d79deb5e`、用户熵减授权去重`0821b506`、M01/M02`ca0de6da`。UUID十一模块684项加定向1+3通过；不能由UUID验收替代SessionThread/执行及E2E。

2026-10-04本次实测 `openspec list --json`：

| change | 勾选进度 |
|---|---|
| add-itemized-rollout-context | 52/87 |
| migrate-identifiers-to-uuidv7 | 49/49 |
| add-unified-virtual-resource-addressing | 5/40 |
| migrate-session-context-uri-to-vrn | 20/35 |
| add-multi-workspace-backend-mounting | 5/34 |
| add-context-injection-lifecycle | 21/57 |
| add-workspace-persistent-resource-management | 2/21 |

不因patch能合并、部分gate绿或接口口头承诺勾大任务。剩余ThreadRuntime/delegate/outbox/resource activation/persistent resource仍在原goal范围，未完成。

## 常驻任务与下一步

表中 worktree 名位于上述 worktree 根，产物名位于上述产物根。状态索引均 `<产物名>/git/status.idx`，报告均该任务 `artifacts/`；E02跨树的唯一产物例外明确列出。

| 任务 / agent | worktree / 产物名 | 当前工作、固定依赖及验证 |
|---|---|---|
| E01b / thread_owner_implementer | thread_owner_implementer / 同名 | HEAD ad093e0c、branch codex/thread-execution-owner，共同ready ef86c12b。一个作者贯穿typed admission→首次notice原子→真实writer membership→middleware/plan/provider/recovery；append_items/ensure_request_items/append_control_commit均完整pair，62定点单测通过，尚无完整fixed交付。R32 ToolSet真实thread intent被main alias gate拒绝已派此作者修；child applied binding也须独立。admissions.py新增必要lint由此作者收口，保回滚失败透明。 |
| E02b-R36 forward / gateway_user_implementer | **integration_fixture_implementer** / **gateway_user_implementer** | HEAD eb938、branch codex/integration-runtime-manifest；完整fixed 0a7972、176paths/中央verify0，73 preimages实hash全部一致，E02-only 59db61→0a7972为55paths，121paths同步前置。R36揭staging root拒thread、clone metadata错pair、assembly seal/Turn acceptance INSERT漏NOT NULL thread，另CSM/seal漏必填session_id、execution turn_index/recovery/model_calls漏pair，原作者按DDL与真实caller集中修闭包；items.py仍E01。index git/e02b-r36-forward.idx，报告e02b-r36-forward-delivery.md；旧50绿不重跑。 |
| E04b-forward/E04c / integration_fixture_implementer | **session_mutation_admission** / integration_fixture_implementer | HEAD5383、branch codex/session-mutation-admission；E04b forward canonical fixture准备入口与五消费者正在验证。asset contract已通过，real provider 2pass/118.41s/Ruff0，三个真实reasoning carrier保留；progressive/partial/boundary Web待验。E04c child NodeDebug drain/main按钮明确尚未实施，不称在途代码。 |
| U03c-forward、R36 / architecture_reviewer | internal_display_projection / 同名 | U03固定58e703→79c2、forward→7e7a（3paths），中央verify0；新增SQLite3pass19deselected、Ruff0，原30+19+60/tsc/build/protocol复用。R38代码未见P1/P2，仅规范漂移；中央同步logical membership fence合同，reader writer仍E01。现只读R36 E02 owner闭包，报告r36-e02-owner-review.md；不独审自己的U03。 |
| O02b-R37 forward / session_vrn_convergence | navigation_outbox_store / 同名 | 原forward 9db→e761，5paths/中央verify0，23unit/tsc/真实Chromium/build0；R37再揭shared flush错归命令outcome与reload跨tab丢远端B两P2，原作者最小增量修。baseline e761，index git/o02b-forward-2.idx，报告o02b-r37-forward-delivery.md。R38 U03只读审已完成，仅规范漂移，未重跑旧绿灯。 |
| O02a-forward / behavior_reviewer | navigation_queue_owner / 同名 | R35独审完成一P2；现修非本人R34两P1，固定preimagea4ba797d、index git/o02a-forward.idx，报告o02a-lifespan-recovery-delivery.md。真实lifespan启动恢复；queue terminal/event与mark deleting同库同事务，后续drain失败仍committed/pending，不回滚。原22+16+真process1pass复用，新受影响入口另验。 |
| O03b / terminal_owner_api_implementer | navigation_authenticated_scope / 同名 | HEAD80cd、branch codex/navigation-authenticated-scope，preimage5418；认证provider 37pass及request_id邻接6pass，主树误投已恢复。真实跨子系统suite迁integration；目录AGENTS明确Gateway与下游独立HTTP进程，复用既有process helper，待O02a显式start/stop同步。主代理曾未读目录指引建议不新增进程，已纠正。后同作者接UI，不新driver。 |
| fixed_delivery_reviewer、uuidv7_implementer、workspace_owner_implementer | 保旧独有树/证据 | 429 errored，不密集恢复或换模型。W/V/UUID已固定历史交付与证据见下文队列和Git；未完成的owner/runtime/resource任务保持goal内，不以模型错误标已完成。 |

## 集成队列与依赖门

1. E02c combined固定 `c9b2577a→4c2d238c`，5paths patch `dd634ba8` 中央strict/hash/raw0，3pass1.99s；旧 `_existing_index_connection(session_id=None)` 属E02b未迁，不能说全面闭合。E02d reader/indexed非空正常路径candidate `6d24717f`、3paths/patch `2e0fbad9` 中央strict3/blob/hash0，真实main/child1pass2.91s；两个productionpaths已同步U/V并Ruff0，真实SQLite测试归integration另delta由E02b处理。
2. E04 root五path固定 `8392b195→a746ed8b`，中央standard patch `f182f78f`，4源码/test live一致，fixture仅目录hunk；104pass为工具转录，无配对raw不重跑补日志。已串行sync T/U/V/E01且Ruff0。A27两P1 gate：NodeDebug canonical全caller原未交；现已交63path fixed增量，见下文最新门。旧catalog迁移仍Session折叠。已裁定删除仅旧JSON/开发兼容迁移链，全caller/fixture/spec完整迁至现行canonical；NodeDebug与旧链删除各交独立delta。
3. E01 overlay写/fork固定 `69d19b5e→589c18a7`、19paths/patch `b624e226`，中央strict19hash0；读侧三元SQL与E02合后验。E01b由一个实现者贯穿admission+首次notice原子、execution/catalog关联、display_only生产membership；R27确认fixture直接INSERT掩盖生产两缺口，旧16+1 reader evidence仍保，不能当入口验收。
4. V02closure `5eb701fb→388705ab`、29paths/patch `c5aaa589`，中央strict29hash0，候选外仅W04七path+OpenAPI两path。A26b Web身份闭包无finding。MessageRead独立 `388705ab→9aff7453`、2paths/patch `a6449f29` 中央strict/hash0，完整read依赖sync后newcaller2pass/21deselected2.81s；assembly pair callee待E01typed、normalindexed待E02d，不整体判pass。
5. 中央E02sync V曾以dependency-only中间base三方合并，把消费树旧内容当删除，静态F821发现；用实际公共祖先eb9380aa重合六共享路径，完整owner模块恢复、Ruff0/import0。E04sync又漏已集成UUID生产前置；sqlite_state与六UUID模块已补T/U/E01，V补ExecutionAdmission/corecontext两模块。只读import不充分，真实creator入口仍须验；事实见coordinator/*prerequisite-sync.json。
6. L02固定 `2323b40e→647cb052`、13paths/patch `10a07a15`，Node12/Python4/lifecycle2/resilience9pass；persistent首次1pass2setup errors，命令env指正式manifest后仅失败2case2pass30.53s。R28确认P1：Gateway launcher传控制面gw_*，Terminal UUID校验启动失败且Python业务UUID不匹配。候选暂停集成；A28比较保持aux先起的workspace-owned identity bootstrap与backend-first方案，后者存在active-goal恢复先于Terminal的竞态。Windows/真实remote未验，不当pass。L01仍主树已集成。
7. 继续W/V预览与当前main合并，保C03/B01/J01/M01/M02/L01及三保护路径，最终统一生成协议/OpenAPI。既有 `529282d3/ea2386e2` 仅preview；不能旧tree替HEAD。ThreadRuntime/delegate/activation/persistent resource仍必要。outbox driver生产检索只定义，接线仍未完成；下一任务按既有量化gate和F1/F2/F4新source判定，勿重复造driver。
8. D01交付helper固定SHA `c6831f673e96e3aa4c13d996334c37df1fbc8a355a4353ea2c19ca6315ad9eca`，Ruff0。D02独立最小真实Git仓库验证binary/mode/新增/删除/未知后缀全6path严格重建、错误Git诊断/私有index回收；发现候选外.proto漏判，静态同根因.ps1/.cjs；中央仅补三后缀，原proto无defer定点exit2、exactdeferexit0且manifest1项差异，ps1/cjs分类true，不复跑原完整绿suite。报告architecture_reviewer/artifacts/d02-delivery-helper-forward-review.md，原raw与修后raw都保。入口/共同流程已有调用参数；工具只证明完整性，业务独审仍必要。


9. E01/E02共同依赖 ready tree `ef86c12b2ce14a02d0218aa022982897c4f9b929`（非commit，E01 HEAD仍ad093e0c）：中央34readpaths与f60前置15paths合并，保typed thread/cache/request content，消除重复参数与child强换main，补overlay SQL/keys/validator三元；Ruff0/import0。第一真实gate3pass1fail（缺checkpoint writer前置），补f60生产后仅失败indexedcase1pass2.76s，原raw都保。E01b开始单实现者贯穿真实reportback/admission/notice/membership；R29最终仅确认旧v1 importer staging read_items旧arity；已授权整条删除旧importer/CLI/tests/spec兼容义务，现行v1拒绝/损坏断言保留。canonical getter签名可疑但沿runner证实生产fallback不可达，撤回P1判定；E01仍按原typed目标收紧callee。
10. E02b caller map307记录，其中connect122/lock64：固定薄壳 `_connect(thread_id, checkpoint_ns, *, session_id: str, read_only=False)`、`_lock(thread_id, checkpoint_ns, *, session_id: str)`，session_id必填、删除fallback。授权单提供者全callee/callsite identity片段含assembly/execution/fork/writer与tests，保E01 admission语义/E04定位；消费者新增调用采用同shape，中央固定后按符号合，不另留可选thread在低层猜main。

11. L02b ready dependency tree `2189b6ec17ab55b3d00d5ea19fbfc97b148b3192`（HEAD仍f478）=fixed647+14个W依赖路径；新增CLI未埋入base。全14Ruff0，首次support import缺process_scope原错误保留，补scope/conftest后production/support import0、相关2casepass0.64s；未跑预期JSON→裸UUID的消费者红灯。A28最终用既有identity module短CLI，不新wrapper、不backend-first；Terminal作者已获完整bootstrap/controller/runtime/testhelper消费+启动/adoption/request闭包，registry仍gw，Browser不扩UUID，保Wowned handles顺序/rollback。记录coordinator/artifacts/l02b-ready-dependency-tree.json。
12. E04 NodeDebug fixed `a746ed8b→e47f8f99bcca136ccf9963e55ba9cbfcb232d1c1`，63path作者patch`5661e878`严格cached重建tree/blobs/hash全过，中央fullindex标准patch`e7878f46`；作者live源码零差异，仅.venv未跟踪。R30行为/A29架构独审已派；作者13unit/1integration/1232Web+build通过，fork4setup缺APIkey不当pass。E04b旧migration删除另delta，以e47为base，caller审计明确两个fixturegen/五integration/pathutils提示与旧reader/layout，继续canonical seeding、保业务断言与v4拒绝，不更改NodeDebug fixed。
13. V新增真实POST Turn→MessageRead selector红灯1failed/15.73s，尚未到read断言；cleanup failures.py→Saver→storage recovery旧pair导致thread_id重复参数，可能覆盖首异常。E01b确认put缺execution_admission_scope、JobID当TurnID及stream/middleware只userturn、append硬编码main，正在同一纵链闭合reportback/Job/Runner/原子notice/membership/recovery。V暂停依赖重跑，原selector/raw保留；独立O01仅审当前Session outbox真实入口/已有driver/APIqueue，区分历史量化与固定main。

14. L02b补齐registry七路径后完整依赖base `ec2fc3d98270c1174bd80034f9aeb63d80043b4d`，fixed candidate `aea29e4ebff9eae0a73f1247d69bdb8c40202f54`、九path/patch `7be5cb3c`，中央strict重建tree及九blob/hash、作者候选外源码为空。首消费者51pass19fail仅转录、raw遗失；修后定点19pass5.00s和新process/CLI25pass4.06s raw/exit0，既绿不补日志复跑。真实managed integration首失败health缺UUID及继承Python配置指错源码树；从唯一WorkspaceService输出health UUID、测试显式选当前Python/cwd后1pass17.53s/exit0。raw在正式 `out/tests/integration/test_managed_terminal_workspace_identity/artifacts/`。A30确认新Terminal readiness仅HTTP200漏UUID，L02c同作者在terminal_owner_api树继续修；Windows/真实remote未验。
15. A29依据 `agent-debug-tool-group/spec.md:445-447` 与 lifecycle design538撤回可选thread的P2：普通Session入口允许catalog解析main，最终同一owner；不强收紧API。R30两真实问题另交E04c：main按钮传alias、Session drain漏child调试runtime。E04b先删旧catalog迁移链。主代理曾误写E04 worktree短名，agent误树只读无写；fresh核真正树为 `session_mutation_admission`/codex/session-mutation-admission，E02树为 `integration_fixture_implementer`/codex/integration-runtime-manifest；证据coordinator/artifacts/e04b-current-worktree-audit.json。已完整重发正确目录/index，不重做已有删除。
16. O01当前outbox审计完成：真实UI未接已有driver，queue FIFO/claim-time/跨进程recovery、非递归delete与IDB跨tab分配均有缺口。O02a由behavior_reviewer转实现，worktree/产物 `navigation_queue_owner`、base d76、branch codex/navigation-queue-owner；范围queue唯一writer/完整owner/recovery，不同时独审自己。O02b由session_vrn_convergence实现，worktree/产物 `navigation_outbox_store`、base d76、branch codex/navigation-outbox-store；同IDBtxn分配+insert、高水位、ID冲突、driver全caller与真实双tab验证。只用Git创建worktree、链接依赖，不复制生成数据。二者fixed后才O03真实UI全入口接线；Gateway导航和文件树另属owner，不借历史Session量化开工。报告沿各任务唯一artifacts根；V selector仍等E01。
17. E02b最终AST仅另作者树items六处缺kw待中央同步，其余RolloutStorage生产/合法test为0；不能按名字把Gateway同名_lock算缺陷。新增52项真实gate24pass/28fail，CSM生产bare Session/main与ResourceActivation fixture别名/SQL漏thread已明确派作者收口。合同完整性与真实身份语义分别验，不因AST0称closed。
18. 主代理W/V/main合成预览仅Git对象/private index，无live/ref变化：d76 union `8aa10630`，V02+W04七protocol blob+SessionService lifecycle/VRN合并 `1aa97dd0`，registry纯静态合入 `504bfab1eeeef77608d8d8a084f54b92ad670c86`。等E01/E02/U/E04/L02完整依赖union、最终protocol/OpenAPI统一gen及入口验收；不可用旧tree替HEAD。
19. 最新完整依赖合成仍仅Git对象/privateindex：W/V/L02 union `db2a24b6`→合E01/E02 ready `f8caa0be`→U03完整前置及U03b `c915d08b`→E04 NodeDebug及A31取消窗口修复 `5ce2a722`→16文件仅import排序 `d4ebc1efa48496845e8c817aa8846d5e948fe247`。347Python AST全过，新Ruff诊断仅16个I001且已消除；既存lint未改语义。保main SessionService M准入/VRN、显式thread与owns_thread，排除ready里的.venv/node_modules及旧buf配置，三保护无diff。主代理已取真实基线/固定对象，未写main/live/ref，未把未完整E01b/E02b算通过。记录coordinator/artifacts/{ready-w-v-l02-resolved-union.json,w-v-l02-e01ready-u-e04-union.json,complete-union-static-audit.json,complete-union-import-fixed.json}。
20. R31 fixed U03b树缺 `InternalExecutionHistoryMixin` MRO，作者运行态16pass不能证明独立fixed tree；完整ready union已有该装配，组合验收仍待。另物理item_sequence分页未证等于membership logicalorder，已派architecture_reviewer为U03c实现者，不独审自己；worktree/产物internal_display_projection，完整live baseline `58e7036e8f3b1f4cab3c9e0af19b11feefdb04df`，仅本轮logicalkeyset/DTO/cursor/必要consumer与反序跨页测试delta。production notice/membership writer仍E01b；保持原reader证据边界。报告u03c-logical-keyset-delivery.md。
21. E04b fixed e47f8f99→e94ba340f29cd1831c150c1b5e090bd74235c3d6，94paths（原陈旧index报101已纠正）、68删除/26postimage，patchSHA ae6ffd8d26a4fb1d02e20bfddd277d1488dd172b5d4baf23cb92748222c9b9a6（原手录已按manifest实hash纠正）；137pass26.77s/Ruff0/OpenSpec40pass、机械作者差异0。中央strict/tree/hash通过，却R33两P1：五fixture仍consume删除的静态session且准备只copy、两asyncio导入丢失。forward已派原作者，原fixed不算通过；首真实asset gate又揭generator引用已删anthropic_messages，需迁deterministic canonical Saver/fixture builder保场景。E04c另收child drain/main按钮，合法可选thread不收紧。
22. A31取消窗口已恢复同步Popen/register无await、中文noqa、Ruff0。R32由Terminal作者独审d4新增装配/依赖，无其它指定入口缺陷；ToolSet真实thread/main gate P1源于e47父树，已分E01b，不能归因import-only merge。报告terminal_owner_api/artifacts/r32-union-dependency-closure.md；fixed_delivery_reviewer仍429，无审查结果。

23. L02c fixed aea29e4e→9eae9f212b4b02909ec446e4a2e17564b7e9a438、2paths、patch22ab1455，中央strict/全机械作者差异0，R33无finding，7unit2.05s+真managed1/17.86s已通过，不复跑。O02a fixedd76→a4ba797d4dca98d2d8d5905dd88ffa873fce9609、11paths/patch4dc88b74，中央strict11hash0；O02b fixedd76→9dbd2a32a348acd5b1c27fbaf18cdab2d5a3a3b3、10paths/patch50d76ebb，中央完整verify0/作者差异0。R34/R35已发现两P1/一P2并退回forward；firstfail raw覆盖如实lost，O02b exit文件2字节是内容0而非exit2，已撤误判。当前main80cd+两个原包预览5418fbc44b15f399fb800cab4b1963c6cf16c9eb、21paths，未写main/ref，forward独审过后先集成，不等大owner union。
24. 完整旧union d4+L02c→37d6796a+E04b→287dc45a+O02a→de92b6d7→a1feb；现精确保main80cd三文档得738600244942b7090473a34678ed9daf125056f9，仍缺E01/E02final/U03c/E04b-forward/E04c/O02bfinal，不能集成当通过。证据coordinator/artifacts/current-composition-previews-80cd.json。main唯一真实tracked差异fork_boundary.py来源未知，blob928f17ee未变；旧c420树不含此path且旧patch错误显示删除，保为invalid证据。fresh私有index纠正保全tree为c53c2498cdc6a3f8565952f548d27fb8d523841c，strict重建一致、patchSHA5961ba72481d7779c3ff4f64432aa28123d1ed4a865cc15d1cbeccb9c7cb2dbd，证据coordinator/artifacts/main-foreign-fork-boundary-fixed-evidence.{json,patch}；此前命令exit1保留。E02否认主根写，不能归责；live首参session_id与E02owner_thread不同，最终核合同，不能直接接纳。
25. R34：worker仅enqueue/await_terminal惰启，restart后无新mutation不恢复；递归删除mark后drain异常仍可rejected（核心偏差base已有、owner切片必须闭合）。R35：B insert完成回调与A拒绝清理交错，旧快照发布漏删disk B并吞新独立C。两作者各修原owner forward、随后交叉增量独审，不复审FIFO/IDB分配已认可部分。
26. O03a实际路由已注册，旧预期404注释过期。partition=gateway_<32hex>+backend标准UUID+user_id/guest；HTTP选择仍gw_<hash>，不能用partition UUID作header。Session create/Fork保独立lifecycle receipt。main导航scope却local/local，O03b先统一authenticated provider；联邦凭据已能验证peer_gateway_id、UserAccess故意None，不将其默认为guest。O03a F4非递归delete旧判定已被O02a固定实现消解，待作者修正。
27. E02b删除CSM MAIN export后的三个生产consumer及test_session_skill_untrack已归E01，作者manifest列精确未满足依赖；它们在E02作者树无实际差异，不能当verify deferred规则。61gate50pass11fail现仅工具转录、raw未找到，如实lost；不补跑既绿50项。U03c当前unit30/integration19/Web60/tsc/build/protocol/Ruff均0，候选79c2d937已冻结；后续forward/R38见35，尚未集成。
28. E02b完整固定eb938→0a7972b94d7671d9063a5eccbcd3f0ce2bc928be、176paths，中央verify0、候选外仅items.py一项真实deferred。19owner/producer unit3.34s、compileall149paths0；Ruff20既有诊断与旧base同，CSM/新fixtures定点0。报告gateway_user_implementer/artifacts/e02b-fixed-delivery.md已中文整理。该包含同步前置，已要求从原73文件preimage记录重建完整pre-edit tree再导本轮delta，避免把旧HEAD整个包当新实施。旧preimage JSON有字面尾部\\n，不能直接json.loads，保invalid原件、输出新规范证据。
29. U03c中央verify25paths/差异0通过；独立probe attempt1仅fixture item_sequence不连续错误；attempt2真实history入口TurnCursorDTO anchor10>internal bound5，exit1/1.52s，原stdout/stderr/exit在coordinator/artifacts/u03c-around-probe-attempt-2.*，探针u03c_around_probe.py Ruff0。根因internal-only MAX误作全view logical坐标fence，canonical window又被clamp；原作者修独立forward，保strict DTO。机械通过不能算行为通过，未集成。
30. O02b-forward作者已验证23定点unit/tsc/真实Chromium matrix/build/diff-check通过，未完成fixed交付；确定性原版红灯restore为[B,C]而预期[C]，修后内存/磁盘/reopen仅C，reload/prune异步期间新本地写保留。O02a-forward已实现mark+queue/event同txn，定点4pass；startup仍需finite ready/owner conflict、后台failed读侧透明，主代理已明确同切片继续修、不新诊断框架。
31. O03b认证合同：当前workspace owner Gateway权威gateway_id、backend自身UUID；local user actor=`user:<user_id>`，guest=`guest`（新租约不改scope），federation=`federation:<verified peer_gateway_id>`且忽略入站actor。HTTP联邦凭据认证的是peer，不冒充下游用户；公开context read/search只剥伪造actor、不新增认证。原作者已接受、实现/验证在途。
32. O03b shell cwd正确但相对apply_patch误投main：作者承认workspace_proxy/session_navigation片段并已撤回；中央fresh index又核proxy_upstream与test_proxy_header_boundaries actor头新增。先保完整tree c821e8a4b7af273d312f2bcab41116f50b244434、小patchSHA201c48f7f177adb6344b96b2f4f07709b4f3a90b4d5deb53e6a48514709236bf并严格重建，live hash再核后仅恢复这两误投路径，Ruff0；证据coordinator/artifacts/main-auth-scope-foreign-preservation.*及main-auth-scope-misdirected-recovery.json。默认index/来源不明fork_boundary未动，正确worktree独有改动保留；重派逐文件绝对编辑目标并恢复执行，首次fixture缺client_label失败raw保。
33. O02b-forward固定9dbd2a32→e76191f4ce5407729645dcc12889b267e564c555、5paths/作者差异0，patchSHA0a786aaac18257b6e10e8541f8c2f97c9fa07e1ea203f24898e22866ea5a8321，中央verify0；23unit/tsc/真实Chromium/build通过，原R35竞态红灯保。gateway作者机械E02-preimage结束后R37独立增量审查，只审新IDdelete/failedclosure/reload/prune，不重跑旧allocation证据。尚未集成。
34. E01 alias consumer21pass4fail精确归CSM旧callee依赖；中央将E02 fixed0a7972的control.py与runtime/context_source_control_state.py串行sync E01，两个pre-live逐字匹配ef86ready、Ruff0，证据coordinator/artifacts/e02b-csm-to-e01-sync.json。仅失败4case依赖闭合后新attempt已4pass，21绿保。Detail fork/remap旧session-only与SQL thread漏列仍E01正在闭合，不扩Runtime。


35. U03 forward 79c2→7e7a2006f9aba3ccb37af92221169d9256529d1b、3paths/patchSHA3f3c27f19082fcf2a7d393f66c4b5f5e31500edb8aa90f80f975671e65608513，中央verify0。R38固定组合只读未见运行时P1/P2，P2规范仍写物理sequence/internal-only上界；中央同步checkpoint-history-loading/design/tasks与附属合同到logical membership、全部可见MAX fence，任务不勾。作者先误投forward报告/index到worktree/out，删除前未hash，正式报告重写事实核对，不能称字节等同搬迁；误文件均不在。
36. E02 73原preimage全部hash匹配，full pre-edit tree4052224ba7ffb190773c7f51eff92fa5701fd729、E02-only59db61e12dcbd377875dd08de3ae8e107edb0e44；full diff56含items deferred回退，E02-only55排items，两标准patch strict重建0a7972一致，176=55owned+121同步前置。规范JSON另存、原invalid保留，scope manifest在gateway_user_implementer/artifacts。R36 AST缺session_id共8都在items E01，两处read caller原manifest漏列已派E01；新增P1交原E02修真实SQL与高层fork/seal/CSM/execution路径，不能用AST/机械pass称writer闭合。
37. R37原作者23case通过仍两P2：独立C共享A flush rejected但实际persisted未入队；reload读跨tab B后本地C publish令identity guard丢snapshot，无保证后续广播。报告gateway_user_implementer/artifacts/r37-outbox-forward-review.md；已派原作者仅修两竞态。E01 typed Detail B另漏ResourceActivationBodyStore owner_thread_id、fork/remap/schema detail thread，作者在收口；plan/snapshot直接测试构造遗漏需完整路径清单另分，Runtime尚未扩。
38. 当前完整union738600+U03组合三方手写冲突已按合同解决成4fd0347d85bb0e4225499f234d9be184f66d92d7，仅private index/对象，无main/ref写。保U03 root/logical必填与断言，Web fixture另保Session VRN/resource身份；6生成path暂保union stage2，最终统一gen，不能当protocol通过。证据u03c-union-conflict-resolution.json。E02-only55对该union strict失败；三方遇已修改的待删migration/builder整体失败且index仍4fd034，无半合候选，证据e02b-owned-union-preview.json，待final E01/E02/E04再合，避免反复重建。
39. B02 64MB副Git库的唯一tree先精确fetch到主对象库 refs/codex/evidence/b02-base-tree，逐mode/blob清单与archive内容核等、无所属进程后删副库，原raw/小diff/报告保留。实际3453文件、4154归档项，历史报告数量不作文件数；首次比较archive整体hash受mtime失败、第二次历史数量断言失败均如实登记，未删除前绕过失败。证据coordinator/artifacts/b02-reproducible-git-copy-cleanup.json。

40. 恢复后的E01作者错误缩scope为items-only，导致E02误以typed Detail/Snapshot/schema无人维护；中央核既有A/B事实重新下发责任并要求两作者给final符号，保独有改动。E02全量owner DML/caller缺pair（turn_index/recovery/model_calls）明确归E02，不因新finding编号再次等待确认。O02a作者55pass/44.38s真实lifespan gate，但只交完成时间摘要、未固定tree/raw引用，已退补机械交付、禁重跑55。中央静态发现两旧部署窗口补交分支，按无旧兼容授权要求核fresh合法状态后删除或修根因，尚未fixed。

## 分工实践比较

用户建议按实际效果择优。先在既有切片记录可比结果，不全面更换分工、不以工具pass推断业务正确。

| 切片与方式 | 首次完整性 / 入口缺陷 / 返工 | 当前判定 |
|---|---|---|
| E02读公共提供者→U/V/E01消费者 | 首次手筛patch残缺被拒；随后cache/indexed缺pair两轮；中央sync缺前置/基线错误又返工 | 固定tree机械交付与完整前置准入先试；主代理合并成本仍高，不能称已解决 |
| L02同作者跨Node/Python，L02b扩至Gateway启动/接管/请求 | 原fixed13paths可重建但R28真实入口失败；L02b缺registry前置19fail，真实启动再揭health/代码来源缺口，修后定点19+25及真managed1pass | L02c固定完整、独审无finding；真入口收口后中央主要合依赖，仍有前置补齐成本，不由一条链宣布最优 |
| E01b单实现者纵向writer→reader | 已改append durable attribution、parent display_only继承，middleware/plan internal branch仍在收口，尚无fixed交付 | 等真实入口/atomic gate/首次交付和新增merge量后比较，不能预称最优 |
| E04b旧catalog删除/fixture消费者 | 机械94paths重建通过且137pass；独审两P1揭五fixture缺seed与两个漏导，forward返作者 | 固定对象提高完整性、不能替代fresh消费者闭包；修后真实gate及主代理补丁量仍待比较 |
| O02按backend/IDB owner并行、交叉独审 | 两个首次固定包均完整且作者差异0，可main strict独立合21paths；独审分别揭启动/逻辑提交两P1与insert回滚一P2 | 机械合并成本低，backend一轮forward仍在验，IDB第二轮forward在途；先验真实竞态再判断，不与E01不同规模直接排名 |
| U03c同作者贯穿reader/DTO/Web | 第一次完整25paths/机械0、30+19+60通过；独审追加空/前分布揭真实around游标P1 | forward3case通过、R38代码无P1/P2但规范漂移，中央手写3path冲突已合；主代理仍需协调生成合同，不能由单链比较判最优 |
| D01机械交付辅助 | 原始L02成功与三负例拒绝；独立D02发现后缀漏判，定点修复复验通过 | 工具可复用，分类规则仍需随语言核验；业务owner仍需独审与真实入口 |

## 目录、服务与清理

系统盘旧副本已删除，迁移释放约238GB；14可重建副本删、67项分类、23092保留文件校验。详见 [目录审查](../../../../../../../docs/handoff/20261003-151500-team-collaboration-directory-review.md)。默认及Drive旧.boxteam分别获授权清空、普通源码保留；仓库根旧terminal-manager和空.boxteam已删除，创建者未证实。

最近删除E01旧snapshot2-base 261222487字节：3428基线matching、32不同/extra、无missing，所有差异保Git tree `1e312a7e`及262120字节patch，strict重建、无使用进程后删；证据 `coordinator/artifacts/e01-snapshot2-cleanup-audit.json`、`e01-snapshot2-cleanup-result.json`及`e01-snapshot2-unique-delta.patch`。当前worktree/未集成独有源未删，后续不复制整树。

服务上次default/Drive ready、前端8027初始化三API200且request_id一致，属历史核验。恢复使用 [migration-state.json](../../../../../../../out/tests/temp/2026/10/03/160552-workspace-migration/coordinator/artifacts/migration-state.json) 原环境：BOXTEAM_PROJECT_ROOT旧入口、BOXTEAM_HOME旧入口/out/development-runtime/boxteam-home、BOXTEAM_DEV_PORT_OFFSET=16；cwd数据盘物理根。重新查dev:status，不能查另一unit或擅启第二组。

## 实测问题、调整与效果

只保当前仍影响恢复的事故及最近关闭证据；更早详情在对应报告和Git历史（本文件ca0de6da版本），不随每轮追加重复规则。

| 证据 | 调整 | 已观察效果与未验证点 |
|---|---|---|
| 多次恢复默认cwd回主树、裸共享status误判D/MM；O03b shell cwd正确却相对编辑误投main | 派单字面绝对workdir/index/artifact/report，编辑也逐文件绝对目标；fresh index核实际diff，保固定tree后纠正 | 前轮误判已撤；本轮误投已保全并恢复两已核路径、作者恢复，不把shell cwd纪律当编辑隔离，后续效果仍待验证 |
| E01/W01/S03重复落worktree/out，E02composition又落T产物；V02 W04与E04新preimages再次落worktree/out，E02首轮3pass/1fail raw遗失 | 唯一字面产物根，消费实hash，现存证据搬回、lost如实标，不补造日志 | e02-9gate raw保正确根；V02 W04搬回并核固定7/7、full可重建baseline删；E04错根6file93766byte正在搬回/去重，路径规则仍观察，不能用“out内”放宽 |
| E01多次patch少callee/ports，原hash一致但绝对header/mixin错导、reader旧arity；中央整file曾覆盖U03codec两行；E02a手筛hunk产生畸形@@并丢核心pair | repo-relative固定candidate+manifest严格重建，全生产callee真实import/signature，共享按符号保其它owner | 6/6、8/8严格hash及真实gate通过；E02首版strict128且21/21目标AST仍旧签名，拒收；v2 standard tree strict30/30+AST/runtime签名0，U三方同步changedfiles Ruff0；R26揭cache/error分支漏pair，E02c进行中。V02closure 388705ab严格29blob/hash通过，fresh比live仅7W04+2OpenAPI，A26b Web无finding；机械化校验脚本D01待实测 |
| E01/T05首次internal notice与admission分事务，metadata误改user_turn；fresh hash混acceptance/admission preimage | 同SQLite owner事务、typed admission决定归属、冲突明确拒；两hash各按正确preimage | hashfix后7pass/2fail到read identity，T05归属冲突独立1pass；原子首notice仍待E01 |
| E02 main+child真实checkpoint发现main折叠Sessionnode，child拒legacy | E04按规范统一创建/locator/resolver全根链，无旧path兼容，不删guard掩盖 | 规范248/253已确认，完整实现/主子并存/reopen尚未验收 |
| W02健康1s误转managed二次spawn、按port杀、构造清理杀adopted；A21b又发现preferred mismatch提前close | 明确attached/managed owner、从首spawn涵盖失败清理、按handle进程组回收，adopt mismatch保旧PID并显式报错 | 37pass、真实handoff1pass、W05两项pass、A21c source无finding；整链待主树集成 |
| W05b真HTTPprocess probe放unit，移integration后tmp_path/workspace仍非正式路径 | 以真实被测边界归层；正式workspace复用镜像路径完整fixture，tmp_path只放runtime临时文件 | W05b2pass且原assert/EOF cleanup保；W05c单项1pass/Ruff0/strict1/1，A21c附录无遗留finding；流程已更新该区别 |
| Web导航把placement耦合pinned | 独立place_under_source默认false，UI子会话动作true，pin保自身语义 | W04route3/WebAPI8/physical1及tsc/build为转录，E2E树含独立fixture；R23无行为finding，但proto缺field真实阻断，V正在补 |
| A22发现Web第二ResourceIdentity/非安全HTTP WebCrypto，selector旁路与query职责混合；R21不完整fanout | 直接后端canonical DTO、统一owner校验/聚焦assembly、incomplete选择明确拒绝，删client派生 | V02fixed27paths→5eb701fb strict27/27与W04→248a1edf过；中央fresh候选比live发现漏37diff，Web仍UUIDv5/WebCrypto。已退回补V02closure全部Web/helper删除/legacyhelper/tests及Message全部readscope；V01旧1229pass不能算修正验收 |
| U03b真实SQLite归unit及mock总数遗漏cap/around/lane/crossowner，raw过期 | 按实际模块边界归integration，逐项真实证据，raw/exit运行时保存，缺依赖暂停测试 | 14实跑9pass5fail原证据齐，fresh三项production/fixture合同问题已接管；修后16pass+新singleexecution1pass，全部raw/真实exit分开保；sparse9pathfixed strict0，独审待；Webdense4pass/build。旧5pass不作14覆盖，lost不补造 |
| A24真实DELETE先Job deleting→compact500，catalog-only测试漏分支 | 复用SessionDeletionPendingError、真实ASGI DELETE holding drain/并发compact，验证checkpoint/compactor零调用 | M02独审无finding，主树40pass、已集成ca0，关闭；不继续重复专项审查 |
| snapshot2重复整源码含32独有差异、261MB | 与Git比对，差异存tree+小patch严格重建，再查进程删副本 | 已回收且独有改动保；不再建整树副本，下一相同场景验证规则 |

另已清掉E02 v1 rejected scratch628KB可复现副本，错误候选保Git tree和标准小patch，strict重建后删除，原失败报告保。证据 coordinator/artifacts/e02a-v1-scratch-cleanup.json。

本状态文件只记真实fixed/验证/集成事实。原始失败集合用于归因；数据清理不能把fresh NOT NULL/owner错误说成旧fixture。独审发现问题先纠正任务并更新已有对应规则，不累积同义禁令。

本轮记忆移动至 GPT 技能同级 `memory/2026/10/04/`，两个团队共用唯一文件，所有入口/AGENTS/交接引用同步；旧顶层文件删除。严重延迟原因在 GPT SKILL.md 简述：残补丁/漏交、公共合同并改/错基线/缺前置、合成fixture掩盖writer、cwd/index/产物名错配。D01新规则效果待真实forward验证，不先宣称解决。
