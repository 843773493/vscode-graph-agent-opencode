# 团队协作实时状态与实测改进

主代理独占维护；稳定规则见 [共同流程](team-collaboration-workflow.md)。更新时间：2026-10-06 本轮（北京时间）。goal **active、无预算上限**；交接与原七个 change 尚未全部验收；本次新增文件工具 change 的规划已写，实施未启动。

## 授权与恢复坐标

- 唯一实现者 `/root/sol_vertical_implementer`，实际模型已核为 **gpt-6.1-sol/max**。Luna 实现者停写；本轮另有 `wire_contract_reviewer` 与 `wire_http_acceptance_reviewer` 两个只读审查者，实际turn_context均为 **gpt-6-luna/max**。前者窄纠正下一wire合同职责；后者118行更正版已主核接受，不重复审查，转查下一epoch/CSM真实恢复入口，产物为TEMP/wire_http_acceptance_reviewer/artifacts/epoch-transition-boundary/report.md。两个原wire报告保留在TEMP同名任务的artifacts/wire-preimage-contract/report.md，观察index在同名git/fixed-0b064.idx。默认技能仍为无前缀 gpt-6-luna/max；本轮试验不等于永久默认，不为常驻数量制造重复实现。
- 用户授权架构熵减、唯一 owner、删除源码开发生成的新旧中间数据、移除旧兼容，不再逐项询问。普通源码、未集成独有改动和来源不明文件保留；不能删 fresh 数据掩盖 writer 缺陷。
- ROOT=`/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。
- TEMP=ROOT/out/tests/temp/2026/10/04/024121-team-execution；A=TEMP/coordinator/artifacts/sol-efficiency-trial。下文证据均相对 A，派单必须展开成完整绝对路径。
- WT=ROOT/out/worktrees/2026/10/04/024121-team-execution/thread_owner_implementer。当前历史迁移固定基线是 tree **0b0642393aee621ccec38a102991a2b3567633ee**；恢复观察 index=TEMP/coordinator/git/r-sol-history-focus-0b064.idx，主已初始化于该tree并恢复同Sol。8e9c113仅为签名早包，不是新baseline。作者只读此index；WT HEAD及观察index都不能代替live，差异显式给固定对象。R最初7f8dc、b086/209481及resume索引仅为历史证据，不再作为恢复坐标。
- 主串行集成与机械冻结导出，作者仅编辑、定向静态分析和运行检查，不改 index/refs、不派生。每命令固定绝对 workdir/index 与 GIT_OPTIONAL_LOCKS=0；不共享 index、不 amend/reset/rebase/pathspec commit。测试始终 matrix 或 timeout+ulimit。
- 三保护：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`。不新建整树副本/archive/.venv，不 /tmp，不复用迁移排除的历史工作区。
- 另一聊天草稿 `docs/handoff/20261005-164605-vrn-directory-navigation-openspec-handoff.md` 按用户新授权只读参考，其原件仍不改写。

已裁定：assembly_ref=ResourceIdentity；unsupported_view；Gateway 创建拒绝保留 ID current；config layer=runtime_override、原 precedence 保留、active/pending 分开；delegate publication 使用 Session SQLite 同事务，不造 Team JSON 权威；internal admission 无 Turn/accepted ingress，保真实 execution link。精确决定见 TEMP/coordinator/artifacts/owner-decisions.json。

## 当前主树与真实运行

本文更新前ROOT HEAD **530449e13c9047dc2ea71d885bcb378c78a0a15e**；最新已提交业务仍 **0e42364606f3c0ef5533de5e35970df82a2ece67**。主树在退休＋reader/terminal/checkpoint209481上再物化display固定tree **0b0642393aee621ccec38a102991a2b3567633ee**，累计owned138路径、业务未提交；最近组合证据基于8f647，tree=`005276b9179afc942f9ef5a68ecaa61055fcbee9`，326e/4fa及其后提交只改交接、规划与技能文档。此前在326e重核ROOT全部138owned内容、mode与删除均仍匹配0b064，两个默认index未变；原checkpoint43＋SQL6的ROOT49验收复用，display另一次ROOT22pass actual0/10.31秒、AST2/Ruff0，不把两次验收相加。主仅机械冻结、装配和独审，业务源码手写0。display原读取owner缺口已窄验，历史加载文件的必填owner/admission迁移另交同Sol，原R其余scope未完；下次恢复重新核HEAD/候选/preimage，不把本文值当live。

主开发服务 unit=`boxteam-dev-cff6686cbc5b0e76.service`，offset16；后端8026、前端8027、Gateway8030。BOXTEAM_HOME=ROOT/out/development-runtime/boxteam-home。此前systemctl核验 **active/running**（本恢复轮未重新核服务），实际 ExecStart 指向 ROOT/scripts/launch/dev.mjs；新增R切片未重启或进行完整产品验收，不将前次启动证据归给新增功能。

授权业务工作区只有 BOXTEAM_HOME/boxteam_workspace 与 `/data1/hyf/test_workspace/drive_bicicle`。typed-startup-attempt-3 整组启动实际0/25.811秒；通过既有 owner API重新登记并 start drive，两者均 managed/ready。从8027实际获取 health/workspaces 与两工作区 `/api/v1/workspace`，均200、响应头体 request_id 一致。证据 `root-product-verification/typed-startup-attempt-3/exit.json` 和 `frontend-api-acceptance-attempt-2.json`。这只证明启动/初始化，不代替后续完整 UI、R或activation验收。真实 Chromium 初始化亦通过（5.9秒、无page error/failed request），证据 `root-product-verification/typed-startup-attempt-3/browser-smoke-attempt-1/receipt.json`。

此前两个120秒失败 raw/exit保留：确证旧生成 workspace/gateway SQLite与共享 `config/workspace-source.sqlite` 仍含旧列，停写与fuser核验后删除。JSONC/.env/identity hash保留。清理收据在启动attempt1与2；新库能实际读写。不保旧兼容，不把清旧数据称源修复。

## 已集成切片与证据

| 切片 | 已兑现范围 | 证据（相对 A） |
|---|---|---|
| 统一资源寻址术语 | ae58a883，162无重命名路径；源码、SQL、API/proto、生成类型、当前规范与引用统一；机制=统一资源寻址，地址=资源 URI/URI，身份=ResourceIdentity，位置=真实路径。无旧别名，boxteam://字节与字段号保留 | uri-terminology-plan/integration-receipt.json；原317/104后端、16/123 Web、50定向组、独立tsc/build与strict40/0复用，重叠不相加 |
| R公共合同 | 8ac，22路径；descriptor root policy与selected ToolSet联合校验、protected完整manifest token修复；独审通过 | r-public-primary-review/final-composed-attempt-1/；ROOT95pass actual0/16.936s，原组重叠不相加 |
| R封存事务前置 | 39509，固定e4d十路径；assembly/source applied/前驱CAS同事务、重试复用原binding；producer/Provider尚未接 | r-seal-transaction-primary-review/root-composed-attempt-1/；ROOT61pass actual0/19.759s，boundary-review scoped pass |
| 来源语义修订号 | 0e423，固定58e1七路径；首次observe→prepare→intent保owner token，content hash独立，重复helper删除 | r-semantic-revision-primary-review/root-composed-attempt-1/；ROOT75pass actual0/14.114s，AST/Ruff0，主独立窄审 |
| typed canonical / importer | 1c20集成完整139路径及两轮恢复：f838→f2c→9bec→8958。全量 typed writer/SQLite/JSONL/checkpoint/fork/provider 与独立 importer；实际 R producers尚待 | typed-canonical-production/final-delivery-attempt-1/；importer-initial-audit-recovery-attempt-1/initial-next-write-recovery-attempt-1/final-attempt-1/primary-mechanical-attempt-1/root-integration.json |
| 持久资源失败重读与竞争 | e38f70f，固定6f93四路径；Session控制失败 await新GET并整体替换、原错误保留、身份/generation fence阻止旧回执污染；真实owner lock/lease竞争。4.3/5.1已勾 | persistent-failure-and-races/final-delivery-attempt-2/；作者19Web/44owner及AST/Ruff/tsc/build；ROOT19Web0/0.316s、build0/16.901s、tsc0/19.552s |
| fork/Runner/Graph/runtime | 0b355主体及4cf1/847e/36e增量已审集成；真实factory/Runner取消与明确owner。compiled reasoning-only/retry最终范围随activation补证 | fork-intent-verification/、runner-verification/、graph-runtime-verification/及对应独审 |
| WVL/catalog/residency | 29513/9fbb完整合同、closing准入、迟到Goal/Job writer、registry持有到释放；9e7013冷读不造slot、成功drain回收、失败保owner | wvl-catalog-owner-verification/attempt-2/、residency-stable-pre-review/、residency-guard/attempt-1/ |
| Terminal/Browser owner | 11a925主体与后续释放、锁及恢复已集成；真实PTY/Chromium、promote、正式Session删除、保留发现及重连等临时产品场景通过 | terminal-owner-production/、browser-owner-production/、root-product-verification/terminal-product-acceptance.json 与 browser-product-acceptance.json |
| 远端真实资源与重启 | 145759远端身份、5fef WS接收、8989fresh连接事务/reader、1de547重启认证consumer。正式attempt7完整Browser/Terminal WS、Gateway重启和fresh SQLite后访问通过，实际0/74.575秒，精确监听无残留 | root-product-verification/remote-browser-terminal-attempt-7/acceptance.json |

严格重建、blob/hash、live preimage、三保护、guard与双祖先已通过。主补业务源码仍 **0**；术语机械迁移按用户授权单列。上述成功不证明外置 unknown 崩溃窗口、正式Web全生命周期、R/activation/delegate或联邦URI全部完成。

## 当前执行与后续顺序

1. **Sol正在实现 R 正向生产切片**，产物 `r-producers-and-toolset-production/`。WT按三方同步163路径后，app/src/tests/scripts/configs/proto/openspec与ROOT1c20零差异；3生成冲突按合并proto等于ROOT后复用官方输出。证据 `root-dependency-sync-attempt-2/preparation.json`；build0/17.483s与真实composition import0/2.990s。不整树覆盖 .codex/docs，不启动 WT/ROOT全服务。
   公共合同8ac、封存事务39509和语义revision0e423已集成；当前固定0b064累计138owned路径已物化ROOT、业务未提交。真实dispatch/API35及Chat双协议正文11项已按固定包通过，不代完整Provider/E2E。旧实验升级退休与reader/terminal/checkpoint已固定并通过窄验；Sol继续受影响history-loading全文caller，随后先固定normalized wire/profile公共合同，再接真实transition与checkpoint CSM恢复。删除旧名、静态候选数量与作者live绿灯均不代整R验收；固定报告及必要测试见下方最新记录。
   R01–R07须从真实 producer→CSM→composition/Saver/compiler→Provider正向接通 typed SourceObservation/provenance/intent；不从最终request反推。首次/fork首root，合法compaction/rewind/toolset_changed新PrefixEpoch；tail_only独立user；并行工具先pending，下一request assistant calls→全部paired results→source。desired/applied ToolSet在安全边界切换。对应 context1.5/1.6剩余、4.6、5.1/5.2/5.4、6.7/7.1及itemized1.5/1.6生产证据；无固定交付前不勾整项。
2. **activation**：现有组件接到真实user/internal执行，唯一freeze owner；assembly_ref=ResourceIdentity，不用execution ID冒充turn，不造internal Turn。保真实资源激活快照、来源控制、sealed重试；复验编译reasoning-only合法body及耗尽收敛。
3. **delegate publication**：Session SQLite publication ledger与child发布同事务，真实factory/start caller与abort/recovery闭合，不倒删published，不造第二JSON账本。 原task对表发现粗队列漏了E01–E07 runtime source/wakeup、同Session team/fanout、通信accepted→binding→cold recovery/持久观察以及8.15背景任务/mailbox；已分成依赖清楚的待办，仍由同Sol后续实现。`create_and_run_internal`已有InternalExecutionIntent，不造真实user Turn；Terminal已有精确source_thread/claim。缺口是pending source与durable wakeup，禁止重建已完成admission。证据 `runtime-event-task-evidence-review/attempt-1/report.md`。
4. **联邦 URI与target-main**：复用现有authority/grant/channel/cursor。已有 GET `/api/v1/sessions/{session_id}/main-thread` 是合同来源，不新增同义endpoint；稳定Gateway authority、受控peer-hub-star最多transit1/hops2/deadline，真实B⇄A⇄C与零副作用拒绝。证据 `remaining-addressing-contract-review/attempt-1/report.md`。
5. **持久资源与最终验证**：1.4/5.3外置owner unknown崩溃窗口后新进程重开，5.2唯一正式WebE2E仍待。固定1c20的harness只读报告为 `persistent-remaining-review/attempt-1/report.md`；实现仍交同Sol。
6. **架构与继承消费者/关闭风险**：固定1c20 task9全63文件owner/import/LOC已独审，主核gateway.py和message_stream_store.py至0e423无差异。9.1证据已勾；9.2按DTO域直接迁import、9.3只提纯reducer，保Store事务顺序，交同Sol后续，不插R。报告 `mounting-architecture-review/attempt-1/report.md`。两个session_navigation旧DTO测试迁到durable operations/receipt，不恢复旧API；通用fixture10秒强杀Gateway父级的关闭风险仍待精确owner handle回收和透明失败，正常attempt7不能证明强杀安全。证据 `remote-cleanup-review/attempt-1/report.md`。最后逐原task/入口验收七change及handoff，不能当前阶段完成就结束goal。

## 实测改进与未证结论

- 固定交付、真实入口与增量早审已实际阻止残缺包落地；同树已过审查按路径/依赖复用，不重审未变全量。125+27path旧审保留；报告baseline转录更正不是源修复。
- importer首份audit曾漏目录创建后退出，首修又漏空/截断next。真实O_EXCL+部分写入+os._exit73的recover/direct retry四红；8958净增9生产行后固定64全文件通过（pytest79.44s、实际0/81.290s），完整/错误身份/未知残留/已发布损坏/恢复自身再次退出均核；规则效果已验证。
- reviewer恢复读错schema3 tree，误撤缺陷；纠正base/target/报告根后回到9bec真实caller/callee。计审查返工，不归Sol；后续效果继续观察。
- 主线程错误诊断“Gateway缺句柄”已撤销。固定7f8dc已有两handle，真实Bun退出3约155ms和SIGTERM均可见，无源码/测试增量。清旧库后服务恢复；此前等待为何未看到退出仍未证明。证据 `dev-launcher-early-exit/final-readonly-attestation.json`。不对反证已推翻的诊断派实现或写镜像测试。
- 主错误成本单列：不存在preload前置失败0.081s、首次依赖patch含已相同新增文件被拒（未写源码）、初始化探针猜错字段connection_state。原raw/exit保留，不计作者返工，不以重试覆盖。
- ROOT共享index当前观察98850fa4…；WT默认index当前4f4eba12…（此前e1cb50fd…）。早前f2afa→98850字节变化，但ls-files --stage逐字相同（stage SHA2ae32540…），发起者未归因、未恢复共享index。每次集成核fresh现场，不能宣称全期字节未变。
- R完整对象早审在生产大包前发现两个P2，forward/protected候选独审与ROOT95绿已验证规则效果；机械完整仍不代联合业务约束。审查中的封存后脱敏假workflow、猜测transition previous字段/source-selected旁路已按实际caller/type及低层合同撤回，计主/审查成本，无作者业务返工，不要求旧算法兼容。e4d scoped pass不代尚未接的生产投影。技能入口规则简短修订，原报告保留。
- 比较数据只维护 `efficiency-comparison.json`；首完整候选、真实入口遗漏、返工、依赖等待、主补救分开计。没有同起点可比试验，不给速度倍率，不宣布永久最佳模型。当前一个Sol贯穿、独审、主串行集成继续；不把继承缺陷、主协调错误或过去Luna投入全归当前作者。

## 当前 R 候选的验证边界

| 固定增量 | 已核范围 | 证据（相对 A） |
|---|---|---|
| d29→6248 | Saver唯一seal、Agent/Bridge真实caller、R01–R07 producer；旧visibility与Skill第二commit链删除。ROOT201pass | r-agent-consumer-primary-review/root-composed-attempt-1/；r-agent-consumer-boundary-review/attempt-1/ |
| ff1/414/5a89/1ff0 | 投影回provider/mapping owner；冻结visibility及AAD/密文配对纠正；offline owner与真实退出恢复。ROOT分别57、189、114、64项，各自有重叠 | r-structure-owner-primary-review/；r-hash-fixture-primary-review/；r-protected-fixture-primary-review/；r-schema-recovery-primary-review/ |
| ce768→7df | source receipt/control、终态取消、Turn恢复、typed detail、snapshot证据调用、active overlay进入compiler；detail在读快照外恢复，保body/hash/guard断言 | r-source-receipt、r-source-control-consumers、r-terminal-consumers、r-turn-recovery、r-typed-detail、r-dispatch-overlay、r-projection-consumers各报告 |
| 180d | 真实Chat/Responses HTTP正文、媒体拒绝及新进程重开，ROOT11pass actual0。完整normalized wire/profile仍待 | r-chat-wire-primary-review/root-composed-attempt-1/；r-chat-wire-boundary-review/attempt-1/ |
| 180d→209481 | 旧实验v2升级与专属tests/helper退休；空选集reader与迟到terminal修复；checkpoint43+unit SQL6，ROOT单轮49pass actual0，AST23/Ruff0 | r-producers-and-toolset-production/legacy-v2-retirement-final-primary-export-attempt-1/；r-legacy-v2-retirement-final-primary-review/root-composed-attempt-1/ |

209481由主机械导出，不假称作者自主交终包。patch SHA=`157c0fd2303e5836bcd300f78dbde10c2a7aa167d1019f7becfef30e4c6bb1fb`；62差异路径/37删除、累计136owned。strict重建、pre/post hash/mode、删除、三保护和两个默认index通过。reader/terminal生产blob与3ba433早审逐字相同；imports102、v1及v1→fork31、typed terminal18与fork未变检查复用，不与49相加。

generic事务/备份恢复/journal/原子DDL、schema10、独立v1 importer十生产路径、普通/protected detail及真实fork保留。冻结schema3.sql只作拒绝/零改写负向输入，SHA=`81d84bd8baef17eb336d3b42babebc8736231b01ccfc3979c4e6459a40b939b2`。schema_v4_fixtures/AGENTS已改只读说明，并非删除；退休目录剩20个pyc核验后清掉，报告保留。

原42pass/1fail、恢复单例1pass保原raw/exit，不拼成43绿。ROOT49是一次完整组合；replay保cp-1完整tool pairing、恰两新item、旧cp-2只旧view、JSONL逐字前缀，hydration四字段按messages SQLite权威行联校，不用canonical时间替代。SQL6case全文逐字从integration移到unit。


Display最终0b064由主机械导出两路径，patch SHA=`96f0823366d1c1e5bddee6cb317a840fad11ae020ba3a4bf6b79756fba55a0dd`；全部16个test函数含decorator AST不变，22参数case保留。生产复用既有require_context_view_order_integrity并去重复unknown-kind查询，session/thread在空结果前校同database_meta。主strict/hash/保护/default index、独立窄审及ROOT22单轮通过，主手写业务源码0；作者新runner的baseline字段仍180d，原记录保留，正式收据以209481→0b064归属，不伪装标签准确。证据 `r-display-primary-review/root-composed-attempt-1/` 和 `r-producers-and-toolset-production/internal-display-final-primary-export-attempt-1/`。

## 下一闭包与尚未完成的合同

| 顺序 | 实际状态及验收 | 证据入口（相对 A） |
|---|---|---|
| history-loading caller | 同Sol在途，base0b064。原两文件组17pass/15fail actual1/299.26秒；全文件必要owner/admission迁移未完。selector首增量d58fcc之后，已知同类签名一次迁完并由主固定8e9c113（仅早包、未物化ROOT/非新baseline），18测试/5helper/140断言保留；139断言逐字，唯一JSONL字节不变断言补必填thread后规范AST仍相同，decorator全部不变，Ruff0。作者selector正式单例1pass actual0/17.041秒复用；主对8e9新签名代表入口首跑actual124/90.023秒、raw空。只读SQLite与JSONL核73checkpoint及第74轮acceptance，确认128轮seed尚未完、未进reader，WT无残留测试进程；seed开销根因未证，不算reader失败或通过、不盲增timeout。证据在signature-representative-attempt-1/stage-diagnostic.json；同固定8e9的低seed真实入口4pass actual0/17.605秒，前后SHA相同（signature-cheap-entry-attempt-1），仅覆盖canonical root/错误final pointer，不代全文；6处put的owner/admission与typed dedup fixture仍待。未变单例不重跑，继续全文、复杂fixture，保分页/预算/不读JSONL/可见性/错pointer语义；cursor陈旧空items期望按冻结include迁轻量摘要。不恢复alias | r-history-loading-primary-review/signature-batch-early-review-attempt-1/（前序selector早包保留）；r-producers-and-toolset-production/checks/session-history-selector-thread-migration-1.* |
| wire/profile | 先固定真实Chat `_create_message_dicts→_astream`及Responses `_responses_payload→aresponses`最终规范化HTTP正文，含projector/version/provider/model/tools/attachment/loss/redaction，按真实载体剔除auth/transport/attempt，drift发送前拒绝。contract修订4及验收增量133行已主核，SHA25aaa65c：完整消息/params唯一转换归agents/providers，ToolSet bridge留原service/provider，Saver编排调用同转换并seal/hash，发送复用产物或全等守卫；不放宽AGENTS、不保现存越界布局、bridge不二次prepare/seal。HTTP更正版118行复用，seal幂等不约束HTTP次数。两个实际反例仍未修：timestamp schema属性type变化被递归删除得到同hash；password/access_token普通schema声明被误认凭据拒绝，而实际顶层api_key明文仍正确拒绝。前者另已证真实Chat参数构造保schema；后者仅域normalizer actual0。均无TCP碰撞/修复证据，不禁止正常属性名。参数未封存、语义字段误删与凭据声明误拒均并下一wire闭包；profile/params传递、非Agent用途及摘要边界仍须实际caller核。主无业务源码改动 | r-wire-preimage-owner-review/attempt-1/；r-wire-normalizer-primary-review/semantic-response-format-attempt-1/；semantic-credential-property-attempt-1/；TEMP/wire_contract_reviewer/artifacts/wire-preimage-contract/report.md |
| transition/CSM | epoch intent仍NotImplemented；view事务未pending transition；prepare未传transition；checkpoint-versioned CSM未恢复。原owner同事务view+CSM+pending，首成功新epoch seal才consume/applied。paired barrier已有rewind/replay/compaction接线，不重做；source_manager live WIP的20测试/120断言/decorator保留、Ruff0；主fail-fast actual1/9.224秒：3pass、1fail，手动restore后seal旧epoch冲突。此fixture未走真实boundary，不单凭其红灯证明生产缺陷；结合原独审推进真实view/CSM/pending事务，固定包及闭包仍待 | r-transition-owner-boundary-review/attempt-1/；r-csm-caller-primary-review/early-wip-review-attempt-1/ |
| ToolSet与后续 | producer已消费新selection并联合seal，不报漏接；SessionControl第二applied只在未接helper/tests，按8.3-B/C清理。activation、delegate publication/runtime source/wakeup、同Session child/team、通信binding/cold recovery/mailbox仍待 | runtime-event-task-evidence-review/attempt-1/ |
| 联邦/继承caller | target-main及联邦URI沿原grant/channel。GatewaySessionContextClient三个旧test缺local_token_supplier待迁，无None旁路；DTO分域/reducer、强杀关闭风险待 | remaining-addressing-contract-review/；local-uri-task-acceptance/；mounting-architecture-review/；remote-cleanup-review/ |
| outbox/最终产品 | Web runtime/worker/flock/generation、旧sync第二writer下线已有，不重做。两pending-parent建child入口、test_catalog_service/test_operations_queue旧caller、App/Gateway/backend真实验收待，8.1/10.8不勾；persistent unknown崩溃及正式WebE2E待 | session-catalog-outbox-current-owner-review/attempt-1/；persistent-remaining-review/attempt-1/ |

正式WebE2E唯一入口 `tests/e2e/clients/web/test_basic_chat_tool_loop.py`。最后按原task ID/真实入口复核七change与handoff；当前绿灯或planning complete不结束goal。`current-task-index.json`和`implementation-queue.json`只作导航。

## 新增规划及技能调整效果

- 新建议已纳四组：统一寻址拥有directory/file/domain_object、files/root、技能多层、分段编码、产物单文件模板和轻量访问；context拥有读取/skill_load分离；Session拥有send/read/wait归一；独立add-unified-resource-file-tools拥有七工具、目标端、条件写/部分/unknown与旧boxteam-session迁移。两处旧规范已直接改写，不只加任务；保原read_file媒体及original/thumbnail、文本Delete File/Move to、两端点preimage及逐路径部分结果，通用目录mutation仍延期。恢复到530449后17规划文件逐项SHA及HEAD blob核验仍与已验9f59888e相同，复用strict41/0，不重复重测；新增22项实施任务全部未勾/未启动。既有收据file-tools-openspec-plan/final-coherence-attestation-attempt-11/及text-patch-preservation-attempt-10/；原strict、skill quick_validate及diff check均actual0。终端/浏览器/团队保业务ID，其余mutation/watch/跨Gatewaycopy/generaloperation_id留后续。
- fixed/direct/helper/间接/deferred分列已发现漏caller；真实callee/MRO规则纠正不存在Saver.main_thread_id阻断28项，固定209481 ROOT49验证checkpoint迁移效果。named旧API归零不能代完整签名闭包。
- 作者无超时rg扫`/`找日志，主核PID/cwd后只SIGTERM该rg，延迟534.29秒；恢复同Sol并重发绝对WT/index/A，入口限定根及外部超时。恢复replay通过，后续自主完整交付效果待证。证据 `sol-unbounded-search-correction/attempt-1/`。
- 两退休reviewer容量错误无新报告，主独审增量并复用删除边界。主误把AGENTS owned路径当删除已撤；旧Gateway缺句柄假诊断、private index/旧tree混读、preload猜错等另计主成本，不归作者/模型。
- Sol对固定tree跑git log/-S得到空结果，不能据此判断提交来源；已纠正为先核对象类型，仅commit核历史，入口原基线条款补一句，后续效果待证。cursor旧items为空断言又与active design:692的冻结include冲突；主独核合同，允许迁为精确轻量摘要/不读完整body，保分页完整性，其它断言仍保留，尚未验收。证据 `skill-object-type-correction/attempt-1/receipt.json`。
- 再次上下文恢复后Sol用裸Git读到相对旧WT HEAD的1057路径及history既有67+/21-历史差异；重发坐标并初始化0b064私有观察index后，真实工具已确认当前history与base相同、display匹配已绿blob，尚未交新增候选。18测试/140断言基线清单在 `r-history-loading-primary-review/baseline-inventory-attempt-1/`；已调整顺序为先一次迁完已明确签名并验证代表入口，再闭合复杂fixture，完整验收不减。证据 `r-history-loading-primary-review/restore-state-index-attempt-1/receipt.json`；不因观察超时或没有shell进程重启活跃agent，不为这次重复问题新增同义技能规则。
- 主用ROOT HEAD观察index跨WT diff误报conftest删除；直接固定blob逐字复核它未变后立即撤回，不要求作者恢复。source_manager测试确有live WIP117行迁移，旧_commit_model_call_pending已归零；固定0b064包仍不包含此WIP，不把两个状态混同。证据 `r-history-loading-primary-review/deferred-direct-blob-check-attempt-1/`。成本计主协调，入口补充对应比较规则，效果仅为此次纠错，后续交付待验。
- 为纠正首增量顺序曾主动中断同Sol，核history SHA仍8b14f12a、无剩余测试进程、两个默认index未变后已恢复，不更换writer/model。证据 `r-history-loading-primary-review/caller-focus-recovery-attempt-1/`。先交已确定的五处user put准入迁移及Ruff，再处理internal-leading与typed dedup特殊fixture；原六处及全文验收范围不减，8e9早包未集成，不以工具观察间隔判agent停止。首批新候选仍待。
- 新域探针核0b064 serialization逐字相同：properties.password/access_token={type:string}误触redaction marker校验，display_name不变，实际顶层api_key仍拒绝。探针初次缺PYTHONPATH导致import退出1，补显式ROOT后actual0、Ruff0；属于主探针配置，不算模型业务返工。原技能字段排除条款只窄加schema声明/凭据值区分，quick_validate0；原reviewer已将三类验收追加同一report，未重复探针。规则后续业务交付及HTTP效果仍待，证据 `r-wire-normalizer-primary-review/semantic-credential-property-attempt-1/`。
- 比较只维护 `efficiency-comparison.json`；继承前置、作者返工、依赖等待、主机械导出和主错误分列。无同起点可比计时，不报倍率/永久最佳模型；继续一个Sol贯穿、独审和主串行集成。

旧流水归档 `skill-state-maintenance/pre-compaction-ac34da2e-current-r209481.md`，只作历史证据，不作为现行指令；更早看Git及原报告。default index观察ROOT=`98850fa4…`、WT=`4f4eba12…`；此前字节变化但stage相同，不称全期精确恢复。每集成核fresh现场。
