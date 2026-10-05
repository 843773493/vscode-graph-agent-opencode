# 团队协作实时状态与实测改进

主代理独占维护；稳定规则见 [共同流程](team-collaboration-workflow.md)。更新时间：2026-10-06 01:07（北京时间）。goal **active、无预算上限**；交接与原七个 change 尚未全部验收；本次新增文件工具 change 的规划已写，实施未启动。

## 授权与恢复坐标

- 唯一实现者 `/root/sol_vertical_implementer`，实际模型已核为 **gpt-6.1-sol/max**。Luna 实现者停写，只读审查按需复用。默认技能仍为无前缀 gpt-6-luna/max；本轮试验不等于永久默认，不为常驻数量制造重复实现。
- 用户授权架构熵减、唯一 owner、删除源码开发生成的新旧中间数据、移除旧兼容，不再逐项询问。普通源码、未集成独有改动和来源不明文件保留；不能删 fresh 数据掩盖 writer 缺陷。
- ROOT=`/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。
- TEMP=ROOT/out/tests/temp/2026/10/04/024121-team-execution；A=TEMP/coordinator/artifacts/sol-efficiency-trial。下文证据均相对 A，派单必须展开成完整绝对路径。
- WT=ROOT/out/worktrees/2026/10/04/024121-team-execution/thread_owner_implementer。R 切片基线 **7f8dc1e385d5e430e4e7a497a03c070a0408a255**；状态 index=TEMP/coordinator/git/r-producers-status.idx，主已实际初始化。WT HEAD 不代表 live 或固定候选。
- 主串行集成，作者仅编辑、freeze/export，不改 refs、不派生。每命令固定绝对 workdir/index 与 GIT_OPTIONAL_LOCKS=0；不共享 index、不 amend/reset/rebase/pathspec commit。测试始终 matrix 或 timeout+ulimit。
- 三保护：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`。不新建整树副本/archive/.venv，不 /tmp，不复用迁移排除的历史工作区。
- 另一聊天草稿 `docs/handoff/20261005-164605-vrn-directory-navigation-openspec-handoff.md` 按用户新授权只读参考，其原件仍不改写。

已裁定：assembly_ref=ResourceIdentity；unsupported_view；Gateway 创建拒绝保留 ID current；config layer=runtime_override、原 precedence 保留、active/pending 分开；delegate publication 使用 Session SQLite 同事务，不造 Team JSON 权威；internal admission 无 Turn/accepted ingress，保真实 execution link。精确决定见 TEMP/coordinator/artifacts/owner-decisions.json。

## 当前主树与真实运行

本文更新前ROOT HEAD **c58b6cab5a6003031fcaac09e2cdc1d85dbe3d02**；业务源码 **0e42364606f3c0ef5533de5e35970df82a2ece67**，其tree=`f7a07cc602fb9c5aeed5bac76eec9bd21f6e2e07`。已集成public0e2a→8ac、transaction e4d→39509、semantic58e1→0e423；下次恢复重新核HEAD，不把本文值当live。后续文档另提交，三保护未变。

主开发服务 unit=`boxteam-dev-cff6686cbc5b0e76.service`，offset16；后端8026、前端8027、Gateway8030。BOXTEAM_HOME=ROOT/out/development-runtime/boxteam-home。本轮systemctl核验 **active/running**，实际 ExecStart 指向 ROOT/scripts/launch/dev.mjs；新增R切片未重启或进行完整产品验收，不将前次启动证据归给新增功能。

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
   公共合同8f922→6dd→0e2a完整22path已经ROOT8ac集成；早审两个P2及protected空policy修复均由同Sol闭合，主95项绿。随后e4d事务10path与58e1语义revision7path分别集成39509、0e423，主61/75相关项绿，独审按未变差异复用；R整体仍未验收。WT当前冻结基点58e1后继续真实prepare→seal→ack→projection，不重审已绿前置。4继承测试疑点与12 owner fixture setup errors完整基线归因未证，未改golden/runtime求绿；必要fixture由同Sol随真实消费者迁移。
   R01–R07须从真实 producer→CSM→composition/Saver/compiler→Provider正向接通 typed SourceObservation/provenance/intent；不从最终request反推。首次/fork首root，合法compaction/rewind/toolset_changed新PrefixEpoch；tail_only独立user；并行工具先pending，下一request assistant calls→全部paired results→source。desired/applied ToolSet在安全边界切换。对应 context1.5/1.6剩余、4.6、5.1/5.2/5.4、6.7/7.1及itemized1.5/1.6生产证据；无固定交付前不勾整项。
2. **activation**：现有组件接到真实user/internal执行，唯一freeze owner；assembly_ref=ResourceIdentity，不用execution ID冒充turn，不造internal Turn。保真实资源激活快照、来源控制、sealed重试；复验编译reasoning-only合法body及耗尽收敛。
3. **delegate publication**：Session SQLite publication ledger与child发布同事务，真实factory/start caller与abort/recovery闭合，不倒删published，不造第二JSON账本。 原task对表发现粗队列漏了E01–E07 runtime source/wakeup、同Session team/fanout、通信accepted→binding→cold recovery/持久观察以及8.15背景任务/mailbox；已分成依赖清楚的待办，仍由同Sol后续实现。`create_and_run_internal`已有InternalExecutionIntent，不造真实user Turn；Terminal已有精确source_thread/claim。缺口是pending source与durable wakeup，禁止重建已完成admission。证据 `runtime-event-task-evidence-review/attempt-1/report.md`。
4. **联邦 URI与target-main**：复用现有authority/grant/channel/cursor。已有 GET `/api/v1/sessions/{session_id}/main-thread` 是合同来源，不新增同义endpoint；稳定Gateway authority、受控peer-hub-star最多transit1/hops2/deadline，真实B⇄A⇄C与零副作用拒绝。证据 `remaining-addressing-contract-review/attempt-1/report.md`。
5. **持久资源与最终验证**：1.4/5.3外置owner unknown崩溃窗口后新进程重开，5.2唯一正式WebE2E仍待。只读reviewer正在固定1c20核现有harness，报告 `persistent-remaining-review/attempt-1/report.md`；实现仍交同Sol。
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

任务导航维护在 `current-task-index.json`，按实际文件刷新，编号任务（含嵌套和字母后缀）与未编号历史审计框分开；数量不证明业务完成。多工作区旧规划 guard及并存读取表述已在9126文档提交收回；只读对表仍以固定1c20检验源码。Gateway当前启动manifest仍只含一个workspace，两个独立backend的绿灯不证明同进程挂载。

旧 WVL Terminal P1核验：d164cf6的Web确实直连Manager且缺owner body；1c20的Web→Workspace Resource API→TerminalResourceOwner→TerminalManagerClient已补齐，历史缺陷已闭合而非过去误报。证据是两个固定对象的完整caller/callee；新task对表不得沿用为剩余缺口。ROOT typed/URI组合真实SQLite/OpenAPI三文件47项通过（实际0/29.831秒），证据 `typed-root-composition-validation/attempt-1/`；不代R生产闭包。

已关闭阶段查文档提交 `e972590dcd0bbe4122aae3bce870e7f72894ed65` 及上述原产物。本次压缩前台账快照保留于 `skill-state-maintenance/pre-compaction-68d597bf20f6.md`，其中“未集成”等记述已被本文当前状态替代；不作为现行指令，不复制回入口。

本地URI task复核：ROOT4d966三服务consumer组合实际1/9.255秒，28项通过、GatewaySessionContextClient三个旧test构造漏必填local_token_supplier失败（maxfail3，不能称整组绿）；14项公开DTO/中央grammar/view/cursor边界探针全过/0.962秒。来源 `local-uri-task-acceptance/attempt-1/`。local模型/入口已实现的旧注释已主修，完整入口验收保持未勾；测试迁移纳同Sol后续消费者，不恢复None认证旁路。config5.4已有源码/原C03及URI317证据，已核记录为完成，source control/activation5.6仍待。ConfigCandidateApplier旧双定义已由中性config/contracts.py单owner闭合，已更正陈旧登记。

2026-10-06固定交付链：public0e2a最终22path→ROOT8ac（95pass）；e4d10path→39509（61pass）；58e1七path→0e423（75pass），各组有重叠不求和。三次AST/Ruff、strict cached重建、ROOT preimage/hash、guard/双祖先/sharedindex均0；主补业务实现仍0，只物化固定blob。protected误报已撤，事务独审scoped pass；生产R真实源冻结、late desired、工具配对屏障、Provider正向投影与Core第二ToolSet权威删除继续由唯一Sol实现。

用户新要求已入规划：统一寻址第8组拥有directory/file/domain_object、files/root与技能包导航、身份/文件分段编码、Session产物单文件模板和轻量读取；context第8组修通用Skill读取与激活分离；Session第9组补send/read/wait目标归一。独立add-unified-resource-file-tools拥有目标端执行、条件写入/部分与未知效果及boxteam-session旧scheme消费迁移，全部新实现保持未勾，不插当前R。固定8ac独审发现旧kind闭集重复表与文本限制误伤原媒体读取；主删除消费者表并补URI→原owner/媒体合同，固定bfc3七路径窄核两项均闭合，严格41/0。已串行提交84f2，证据 `file-tools-openspec-plan/closure-attempt-5/`。保留原handoff草稿，不称新增功能已实现；终端/浏览器/团队继续各自业务ID。活跃memory按北京时间跨日移动到06唯一节点，两个入口引用同步，无重复旧副本。

文件规划提交3940（17路径）、f107边界与附件delta、84f2媒体/唯一owner修订；最新strict41/0、独审finding闭合、guard/双祖先/sharedindex不变。文件规划本身未修改业务源码或旧handoff草稿；本轮原R的e4d/58e1业务集成单独计证。

本轮规划原稿的媒体范围遗漏与重复kind表归主规划/审查成本，源码实现尚未启动。严格校验只能验证结构，语义矛盾由固定对象交叉审发现；保留原report，后续仅重核新增差异。

本轮补齐context旧验收7.3-L：合法Skill文件URI可读且零activation/tracking，source preparation/Provider dispatch保持零文件I/O；旧私有路径拒绝，skill_load仍冻结来源。它此前与新spec冲突，属于主规划返工，不归Sol。

R early 19path仍待固定交付；作者live新增7项通过（actual0/13.023秒、AST/Ruff0），主已读原command/raw/exit，但不归固定候选或整R验收。freeze被WT默认index字节变化阻止；主独立读3445项mode/blob/stage与WT HEAD ad093e完全一致、读前后SHA4f4eba不变，允许按新SHA续guard，未恢复默认index。只证明当前staging等于HEAD，不证明原字节精确恢复或完整历史命令。证据 `r-producers-and-toolset-production/production-prefix-early-attempt-1/primary-index-incident-review.json`。

owner_contract当前ROOT c58定向89pass后首个setup error（actual1/15.834秒）来自owned_included_snapshot canonical ref public与entry默认internal冲突。独审已定位最小合法fixture迁移，保guard/断言，交唯一Sol随消费者完成；无独立旧baseline，不作继承归因。证据 `owner-contract-fixture-verification/attempt-1/fixture-review.md`。Browser6.4架构独审仍在途，不插当前R。

旧验收7.3-L修订后，strict全量41pass/0fail、两版本skill quick_validate actual0；证据 `file-tools-openspec-plan/acceptance-coherence-attempt-6/`。这是规划/技能校验，不代文件工具实施或E2E验收。
