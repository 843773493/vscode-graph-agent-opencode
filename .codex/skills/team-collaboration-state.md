# 团队协作实时状态与实测改进

两个团队入口共用，主代理独占写入。恢复、派单、集成及报错时读本文件；稳定规则只在 [共同流程](team-collaboration-workflow.md) 维护。更新时间：2026-10-04 11:22（北京时间）。goal active、无预算上限，交接及七个 change 的必要实现、独审、验证、清理和技能实测仍未全部完成。

## 当前授权与现场

- 用户授权按架构熵减自主统一 owner、显式身份和单链路，删除旧兼容；源码开发生成的新旧中间数据可直接删除，不再询问。保普通源码、未集成独有改动、来源不明业务文件；不能用删数据掩盖 fresh writer 缺陷。
- GPT 团队精确 `gpt-6-luna / max`，不带前缀；主模型由用户选。至少3、最多15常驻，主代理统一派单/审查/集成。现有九个 agent 的创建参数与实际模型均已核，证据 `coordinator/artifacts/model-context-latest.json`、`u05-model-context.json`。429保源和真实错误，不换模型、不密集恢复。
- 物理根 `/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。活动 worktree 根 `out/worktrees/2026/10/04/024121-team-execution/`，产物/索引根 `out/tests/temp/2026/10/04/024121-team-execution/`。下文路径相对此物理根；每次派单仍给字面绝对 workdir/index/artifact/report。
- 主树 HEAD `ca0de6da253fab3422049ac89809cfdbdfa2153f`。M01/M02已集成，40pass、Ruff0、A24b无finding、guard/祖先0。恢复时用 fresh 独立索引核真实状态；共享 `.git/index` 极陈旧，裸 status 的 D/MM 不能当源码差异。本文件和流程当前为主代理文档在途，不交给 subagent。
- 主代理唯一串行提交者，索引 `coordinator/git/integration.idx`；fresh read-tree→精确add→record→commit无pathspec→verify→祖先。禁amend/reset/rebase/push与共享index写入。
- 三处受保护路径：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`，不能修改。
- 测试/probe用matrix或外部 `timeout N bash -c 'ulimit -d 4194304; exec "$@"' bash ...`。uv/bun；保存raw stdout/stderr及实际exit。已pass不为补日志重跑；缺失raw如实lost/转录。源码改后静态，Web改后build。
- 已裁定：`assembly_ref=ResourceIdentity`；统一owner拒绝码`unsupported_view`；Gateway ID `current`创建拒绝；配置layer表示逻辑来源，sqlite读标签改runtime_override、优先级不变、快照独立。delegate复用Session SQLite collaboration ledger与publication同事务，不另造Team JSON权威。internal admission无Turn/accepted ingress，semantic输出保原，execution link显式。证据 `coordinator/artifacts/owner-decisions.json`。

## 集成事实与范围

交接：[bug hunt](../../docs/handoff/20261003-060700-bug-hunt-and-redundancy-handoff.md)。已集成T01、G01/G02、S01/S02、U01/U02、T04、G03、配置来源/C03、S03/S04、UUID自然allocation/U06/U07、B01/B02/J01、S06-v2、M01/M02；闭合历史以Git与对应报告为准，不复跑完整绿灯矩阵。近期提交：UUID真实回拨`b75dbd90`、B01/B02/J01`28359675`、S06-v2`5383baf3`、稳定流程分离`d79deb5e`、用户熵减授权去重`0821b506`、M01/M02`ca0de6da`。UUID十一模块684项加定向1+3通过；不能由UUID验收替代SessionThread/执行及E2E。

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
| E01 / thread_owner_implementer | thread_owner_implementer / 同名 | 基线ad093e0c、branch codex/thread-execution-owner，原ordered writer链f2a6f744+hashfixc25ed561→69d19b5e已同步T/U。429在途129file保存35c4d3eb及coordinator/e01-429-current.*，未accepted；本轮同模型恢复一次。typed plan/assembly/ToolSetRef/DetailRef mandatory thread，pair producer/consumer/FK/key、DetailStore/fork/remap/GC和overlay writer正在收口。source_overlays DDL新增NOT NULL thread_id、epoch index三元、validator增thread_id，作者实源码已落但尚无fixed patch。source row/header须与明确源pair校验、target从materialization pair传；session-only fork_compaction公共边界可catalog取main，底层不猜。首次notice+admission同事务、Runner/factory/JobStep完整入口仍待。preimage唯一artifacts/e01-typed-owner-preimage；完整闭包授权，不因跨层逐个等确认 |
| E02a/E02b / gateway_user_implementer | integration_fixture_implementer / **gateway_user_implementer** | 在T05树基线eb9380aa独占read符号；所有preimage/log/report只能写gateway产物根。17+read生产闭包含snapshot、items/view、anchors、history、fork/reconcile、registry/composition及schema/offline。open_snapshot(session_id,thread_id,checkpoint_ns)，snapshot强制pair；langgraph用MutationIntentOwner+session_thread_owner_scope，不能把thr作session。九gate9pass/3.95s raw已保，新增caller又暴露view/read_items旧arity，优先交正常read callee闭包fixed patch/manifest/import-signatures，尚未交。E02b完整迁history20 raw saver调用、maintenance和nosnapshot callers，不留compat。list_source_overlays(session_id,thread_id,*,source_overlay_epoch,checkpoint_ns,snapshot)，SQL三元owner，validator传thread_id并mapping返回它；DDL归E01、主代理fixed后sync。E01/U/V此刻尚未同步E02，缺依赖不反复重测 |
| E04 / integration_fixture_implementer | session_mutation_admission / 同名 | 基线固定M01/M02 tree8392b195，M六文件及产物冻结只读，新artifact e04-*。E02 sameSession真实main+child暴露main折叠Sessionnode、child误判legacy；规范rollout-checkpoint-storage248/253已要求main threads/mainID、child冻结日期locator。E04统一resolver/SessionCreation发布、durability/proof/recovery及全部合法caller/fixture；NodeDebug保产品功能、入口明main，不让底层accept伪thread alias；rollout_fork_recovery.py NodeDebug target_node定位片段及四个旧物理路径测试已扩给E04，E01不抢该定位符号。不新增rollout扫盘判断到resolver、不仅删staleguard。根fixed依赖尚未交；两SQLite/JSONL并存/reopen互不影响待验 |
| U03b / uuidv7_implementer | internal_display_projection / 同名 | 基线7596a841；原U03source9f5bba3c/tests8c34c823冻结不改，E01依赖真实import/signature已同步、primitives独有合同保。原7Python/Web37+tsc/build通过为转录，明确推断。新realSQLite14case已编：256/257cap、0Turn300分页、gapped dense around、head/tail/lane exhaustion、cross-owner/v1拒、坏root/order等；新Python14尚未执行，等待E02a实际read依赖，不跑旧arity。Web新dense mixed4pass/build/Ruff0；旧raw TypeError lost，不补造。fixture正式bundle/resolver/DDL与JCSoffset/hash，不复制分页算法 |
| V02 / session_vrn_convergence | session_vrn_owner / **session_vrn_convergence** | V01固定e8b96590，54file b3d4c440+defc0ea1中央54/54hash，56unit/11HTTP/1229Web/1Chromium已通过，不重跑全矩阵。新429在途18file保82cc91b1未accepted，本轮同模型恢复一次。V02Web直接SessionDTO resource_identity/vrn/scope、assembly_ref selector，删除UUIDv5/WebCrypto/helper与backendWorkspaceIdprops全部caller；Gateway统一view/selector、incomplete fanout拒绝唯一选择、assembly聚焦拆分。V02a独立项可先交，V02b后端pair callee依赖E01/E02。message state显式pair但本树langgraph仍旧，E02未sync，不自行复制第二Saver。删旧_migrate_legacy_workspace_ids及独用constant/writer，全caller零引用；坏ws_local测试改初始化不写manifest、读取明确拒绝，合法断言保。W04 proto optional bool place_under_source=5授权V唯一writer，单独patch/manifest，实际gen/tsc/build；W请求schema片段不覆盖 |
| W01–W05c / workspace_owner_implementer（429，主代理接管补测试） | workspace_owner_implementer / 同名 | 基线ad093e0c，codex/workspace-session-mount。W chain原a0197661，W02 ownership ab86e1cf strict6/6→07406f15、37pass；W03真实supervisorhandoff1pass保Browser PID/page。W04 f05f039b strict6/6→48495b8d，独立place_under_source默认false与pinned分离，R23行为无finding但proto缺field阻断。W05 9eaa4d30 strict2/2→0faf8a6b，adopt既有API首选port不匹配明确失败保PID，不spawn；显式replacement false保原行为，2pass/A21c无source finding。作者新429不密集恢复。主代理W05b a54ef927 strict2/2→15cbc9c0把真HTTP adoption probe移integration，stdin EOF及kill fallback清理、2pass/Ruff0。A21c新增正式workspace路径P2→W05c 8e7a298b strict1/1→857eff47复用integration_workspace_root_path完整fixture，单项1pass1.16s/actual0/Ruff0；A21c附录无遗留finding |
| M01/M02（已集成） / integration_fixture_implementer转E04 | session_mutation_admission / 同名 | M01 aaafaa83 strict4/4→af816a91；M02 985acbf4 strict2/2→8392b195，M01不变。Session短写topology shared→single session exclusive→fresh catalog active，释放topology后保Sessiongate；compact/goal/update统一gate。真实DELETE先Job deleting冲突复用SessionDeletionPendingError，不500。A24b无finding、主树40pass24.55s/Ruff0，commit ca0de6da/audit coordinator/m01-m02-main-*；原82pass/1fail基线409/422已18a修并定向4pass，不再重跑 |
| A21c / architecture_reviewer | 主树只读 / 同名 | A21c W05源码无finding。W05b delta原进程断言/cleanup保真，但发现tmp_path/workspace不符正式镜像规则；主代理W05c已修，附录已核fixed857eff47，无遗留finding。报告artifacts/a21c-w05b-adoption-test-addendum.md；不重跑37或旧2项 |
| R23/R24 / behavior_reviewer | 主树只读 / 同名 | R23固定W04 placement/pin无新finding，阻断proto缺field，artifacts/r23-session-fork-navigation-review.md。R24当前只读ca0de6da Terminal owner API真实caller闭包：list_terminals_from_state及sync/async/Node校验、精确最小切片与保留断言，报告artifacts/r24-terminal-owner-api-closure.md；不泛盘点、不实施、不跑测试。A23旧数据迁移建议已被最新用户授权覆盖 |
| canonical_allocation_implementer（旧429） | 已回收uuid_live_allocation / 原证据保留 | U05/U06/U07已集成独审/验证，旧429不恢复；49/49 UUID已完成。旧worktree独有差异已保，不能另造重复implementation |

## 集成队列与依赖门

1. 收E02a正常生产read闭包fixed patch/manifest，strict candidate/hash/import后由主代理串行sync U/V/E01相应符号；保primitives/message/query其它owner片段，再通知U03b跑新14case。
2. 收E04 main独立线程节点创建/resolver/caller/proof fixed，真实主子并存/reopen独审后sync E01/E02/U/V；E02历史20与完整read gates随必要writer依赖闭合。
3. E01 typed plan/detail/overlay writer及DDL fixed与E02三元SQL同步，首次notice原子/真实JobStep执行后续仍待，不能以口头签名当依赖。
4. V02独立Web/Gateway/resolver先fixed审，pair backend后续；W04 field5独立协议增量与真实生成不可漏。
5. W/V旧base preview `5c83f1a6`仅预览。主代理已用git merge-tree与ca0主树三方合并→`529282d3`，M01/M02六path逐片段保留、三保护路径不变；再strict加W05c→`ea2386e2`，证据 `coordinator/artifacts/w-v-ca0-main-preview.json`、`w-v-ca0-w05c-preview.json`。均未集成，待V02/E04/E02及central统一gen。不能把旧tree直接替当前HEAD，必须保C03/B01/J01/M01/M02。
6. 已授纵向链路caller完整迁，共享文件按符号合并，提供者不自行写消费者树。独审统一由主代理排队，实现者只提交请求。
7. Terminal后续按R24给出Node唯一owner API链，ThreadRuntime依赖真实execution pair；outbox/delegate/activation尚未启动新实现，仍属原goal必要项。

## 目录、服务与清理

系统盘旧副本已删除，迁移释放约238GB；14可重建副本删、67项分类、23092保留文件校验。详见 [目录审查](../../docs/handoff/20261003-151500-team-collaboration-directory-review.md)。默认及Drive旧.boxteam分别获授权清空、普通源码保留；仓库根旧terminal-manager和空.boxteam已删除，创建者未证实。

最近删除E01旧snapshot2-base 261222487字节：3428基线matching、32不同/extra、无missing，所有差异保Git tree `1e312a7e`及262120字节patch，strict重建、无使用进程后删；证据 `coordinator/artifacts/e01-snapshot2-cleanup-audit.json`、`e01-snapshot2-cleanup-result.json`及`e01-snapshot2-unique-delta.patch`。当前worktree/未集成独有源未删，后续不复制整树。

服务上次default/Drive ready、前端8027初始化三API200且request_id一致，属历史核验。恢复使用 [migration-state.json](../../out/tests/temp/2026/10/03/160552-workspace-migration/coordinator/artifacts/migration-state.json) 原环境：BOXTEAM_PROJECT_ROOT旧入口、BOXTEAM_HOME旧入口/out/development-runtime/boxteam-home、BOXTEAM_DEV_PORT_OFFSET=16；cwd数据盘物理根。重新查dev:status，不能查另一unit或擅启第二组。

## 实测问题、调整与效果

只保当前仍影响恢复的事故及最近关闭证据；更早详情在对应报告和Git历史（本文件ca0de6da版本），不随每轮追加重复规则。

| 证据 | 调整 | 已观察效果与未验证点 |
|---|---|---|
| 多次恢复默认cwd回主树、裸共享status误判D/MM或“并发回滚”；最近U03b/V02又发生 | 派单提供字面绝对workdir/index/artifact/report、写前核物理cwd/Git顶层/branch、按fixed git show归因 | 已撤误判，未证实越界source写入；每次命令cwd纪律仍未稳定，不能称已解决 |
| E01/W01/S03重复落worktree/out，E02composition又落T产物；E02首轮3pass/1fail raw遗失 | 唯一字面产物根，消费实hash，现存证据搬回、lost如实标，不补造日志 | e02-9gate raw保正确根；路径规则仍观察，不能用“out内”放宽 |
| E01多次patch少callee/ports，原hash一致但绝对header/mixin错导、reader旧arity；中央整file曾覆盖U03codec两行 | repo-relative固定candidate+manifest严格重建，全生产callee真实import/signature，共享按符号保其它owner | 6/6、8/8严格hash及真实gate通过；E02生产items/view闭包尚未fixed，依赖口头说明不能消费 |
| E01/T05首次internal notice与admission分事务，metadata误改user_turn；fresh hash混acceptance/admission preimage | 同SQLite owner事务、typed admission决定归属、冲突明确拒；两hash各按正确preimage | hashfix后7pass/2fail到read identity，T05归属冲突独立1pass；原子首notice仍待E01 |
| E02 main+child真实checkpoint发现main折叠Sessionnode，child拒legacy | E04按规范统一创建/locator/resolver全根链，无旧path兼容，不删guard掩盖 | 规范248/253已确认，完整实现/主子并存/reopen尚未验收 |
| W02健康1s误转managed二次spawn、按port杀、构造清理杀adopted；A21b又发现preferred mismatch提前close | 明确attached/managed owner、从首spawn涵盖失败清理、按handle进程组回收，adopt mismatch保旧PID并显式报错 | 37pass、真实handoff1pass、W05两项pass、A21c source无finding；整链待主树集成 |
| W05b真HTTPprocess probe放unit，移integration后tmp_path/workspace仍非正式路径 | 以真实被测边界归层；正式workspace复用镜像路径完整fixture，tmp_path只放runtime临时文件 | W05b2pass且原assert/EOF cleanup保；W05c单项1pass/Ruff0/strict1/1，A21c附录无遗留finding；流程已更新该区别 |
| Web导航把placement耦合pinned | 独立place_under_source默认false，UI子会话动作true，pin保自身语义 | W04route3/WebAPI8/physical1及tsc/build为转录，E2E树含独立fixture；R23无行为finding，但proto缺field真实阻断，V正在补 |
| A22发现Web第二ResourceIdentity/非安全HTTP WebCrypto，selector旁路与query职责混合；R21不完整fanout | 直接后端canonical DTO、统一owner校验/聚焦assembly、incomplete选择明确拒绝，删client派生 | V02源码在途，gen/非安全HTTP完整验证未交；V01旧1229pass不能算修正验收 |
| U03b真实SQLite归unit及mock总数遗漏cap/around/lane/crossowner，raw过期 | 按实际模块边界归integration，逐项真实证据，raw/exit运行时保存，缺依赖暂停测试 | 14新case已编但尚未执行；Webdense4pass/build。旧5pass不作14覆盖，lost不补造 |
| A24真实DELETE先Job deleting→compact500，catalog-only测试漏分支 | 复用SessionDeletionPendingError、真实ASGI DELETE holding drain/并发compact，验证checkpoint/compactor零调用 | M02独审无finding，主树40pass、已集成ca0，关闭；不继续重复专项审查 |
| snapshot2重复整源码含32独有差异、261MB | 与Git比对，差异存tree+小patch严格重建，再查进程删副本 | 已回收且独有改动保；不再建整树副本，下一相同场景验证规则 |

本状态文件只记真实fixed/验证/集成事实。原始失败集合用于归因；数据清理不能把fresh NOT NULL/owner错误说成旧fixture。独审发现问题先纠正任务并更新已有对应规则，不累积同义禁令。
