# 团队协作实时状态与实测改进

主代理独占维护；稳定规则见 [共同流程](team-collaboration-workflow.md)。更新时间：2026-10-05 22:47（北京时间）。goal **active、无预算上限**；交接与七个 change 尚未全部验收。

## 授权与恢复坐标

- 唯一实现者 `/root/sol_vertical_implementer`，实际模型已核为 **gpt-6.1-sol/max**。Luna 实现者停写，只读审查按需复用。默认技能仍为无前缀 gpt-6-luna/max；本轮试验不等于永久默认，不为常驻数量制造重复实现。
- 用户授权架构熵减、唯一 owner、删除源码开发生成的新旧中间数据、移除旧兼容，不再逐项询问。普通源码、未集成独有改动和来源不明文件保留；不能删 fresh 数据掩盖 writer 缺陷。
- ROOT=`/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。
- TEMP=ROOT/out/tests/temp/2026/10/04/024121-team-execution；A=TEMP/coordinator/artifacts/sol-efficiency-trial。下文证据均相对 A，派单必须展开成完整绝对路径。
- WT=ROOT/out/worktrees/2026/10/04/024121-team-execution/thread_owner_implementer。R 切片基线 **7f8dc1e385d5e430e4e7a497a03c070a0408a255**；状态 index=TEMP/coordinator/git/r-producers-status.idx，主已实际初始化。WT HEAD 不代表 live 或固定候选。
- 主串行集成，作者仅编辑、freeze/export，不改 refs、不派生。每命令固定绝对 workdir/index 与 GIT_OPTIONAL_LOCKS=0；不共享 index、不 amend/reset/rebase/pathspec commit。测试始终 matrix 或 timeout+ulimit。
- 三保护：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`。不新建整树副本/archive/.venv，不 /tmp，不复用迁移排除的历史工作区。
- 另一聊天未跟踪草稿 `docs/handoff/20261005-164605-vrn-directory-navigation-openspec-handoff.md` 不碰。

已裁定：assembly_ref=ResourceIdentity；unsupported_view；Gateway 创建拒绝保留 ID current；config layer=runtime_override、原 precedence 保留、active/pending 分开；delegate publication 使用 Session SQLite 同事务，不造 Team JSON 权威；internal admission 无 Turn/accepted ingress，保真实 execution link。精确决定见 TEMP/coordinator/artifacts/owner-decisions.json。

## 当前主树与真实运行

ROOT 源码 HEAD **1c20cc572039e8905483c0c8646a46d69f38055f**，tree=`621b97724cef1da73951d6a96bf4b16d6a7ad3d6`。下次恢复重新核 HEAD，不把本文值当 live。

主开发服务 unit=`boxteam-dev-cff6686cbc5b0e76.service`，offset16；后端8026、前端8027、Gateway8030。BOXTEAM_HOME=ROOT/out/development-runtime/boxteam-home。当前整组 **active**，实际 ExecStart 指向 ROOT/scripts/launch/dev.mjs。

授权业务工作区只有 BOXTEAM_HOME/boxteam_workspace 与 `/data1/hyf/test_workspace/drive_bicicle`。typed-startup-attempt-3 整组启动实际0/25.811秒；通过既有 owner API重新登记并 start drive，两者均 managed/ready。从8027实际获取 health/workspaces 与两工作区 `/api/v1/workspace`，均200、响应头体 request_id 一致。证据 `root-product-verification/typed-startup-attempt-3/exit.json` 和 `frontend-api-acceptance-attempt-2.json`。这只证明启动/初始化，不代替后续完整 UI、R或activation验收。

此前两个120秒失败 raw/exit保留：确证旧生成 workspace/gateway SQLite与共享 `config/workspace-source.sqlite` 仍含旧列，停写与fuser核验后删除。JSONC/.env/identity hash保留。清理收据在启动attempt1与2；新库能实际读写。不保旧兼容，不把清旧数据称源修复。

## 已集成切片与证据

| 切片 | 已兑现范围 | 证据（相对 A） |
|---|---|---|
| 统一资源寻址术语 | ae58a883，162无重命名路径；源码、SQL、API/proto、生成类型、当前规范与引用统一；机制=统一资源寻址，地址=资源 URI/URI，身份=ResourceIdentity，位置=真实路径。无旧别名，boxteam://字节与字段号保留 | uri-terminology-plan/integration-receipt.json；原317/104后端、16/123 Web、50定向组、独立tsc/build与strict40/0复用，重叠不相加 |
| typed canonical / importer | 1c20集成完整139路径及两轮恢复：f838→f2c→9bec→8958。全量 typed writer/SQLite/JSONL/checkpoint/fork/provider 与独立 importer；实际 R producers尚待 | typed-canonical-production/final-delivery-attempt-1/；importer-initial-audit-recovery-attempt-1/initial-next-write-recovery-attempt-1/final-attempt-1/primary-mechanical-attempt-1/root-integration.json |
| 持久资源失败重读与竞争 | e38f70f，固定6f93四路径；Session控制失败 await新GET并整体替换、原错误保留、身份/generation fence阻止旧回执污染；真实owner lock/lease竞争。4.3/5.1已勾 | persistent-failure-and-races/final-delivery-attempt-2/；作者19Web/44owner及AST/Ruff/tsc/build；ROOT19Web0/0.316s、build0/16.901s、tsc0/19.552s |
| fork/Runner/Graph/runtime | 0b355主体及4cf1/847e/36e增量已审集成；真实factory/Runner取消与明确owner。compiled reasoning-only/retry最终范围随activation补证 | fork-intent-verification/、runner-verification/、graph-runtime-verification/及对应独审 |
| WVL/catalog/residency | 29513/9fbb完整合同、closing准入、迟到Goal/Job writer、registry持有到释放；9e7013冷读不造slot、成功drain回收、失败保owner | wvl-catalog-owner-verification/attempt-2/、residency-stable-pre-review/、residency-guard/attempt-1/ |
| Terminal/Browser owner | 11a925主体与后续释放、锁及恢复已集成；真实PTY/Chromium、promote、正式Session删除、保留发现及重连等临时产品场景通过 | terminal-owner-production/、browser-owner-production/、root-product-verification/terminal-product-acceptance.json 与 browser-product-acceptance.json |
| 远端真实资源与重启 | 145759远端身份、5fef WS接收、8989fresh连接事务/reader、1de547重启认证consumer。正式attempt7完整Browser/Terminal WS、Gateway重启和fresh SQLite后访问通过，实际0/74.575秒，精确监听无残留 | root-product-verification/remote-browser-terminal-attempt-7/acceptance.json |

严格重建、blob/hash、live preimage、三保护、guard与双祖先已通过。主补业务源码仍 **0**；术语机械迁移按用户授权单列。上述成功不证明外置 unknown 崩溃窗口、正式Web全生命周期、R/activation/delegate或联邦URI全部完成。

## 当前执行与后续顺序

1. **Sol正在实现 R 正向生产切片**，产物 `r-producers-and-toolset-production/`。WT按三方同步163路径后，app/src/tests/scripts/configs/proto/openspec与ROOT1c20零差异；3生成冲突按合并proto等于ROOT后复用官方输出。证据 `root-dependency-sync-attempt-2/preparation.json`；build0/17.483s与真实composition import0/2.990s。不整树覆盖 .codex/docs，不启动 WT/ROOT全服务。
   R01–R07须从真实 producer→CSM→composition/Saver/compiler→Provider正向接通 typed SourceObservation/provenance/intent；不从最终request反推。首次/fork首root，合法compaction/rewind/toolset_changed新PrefixEpoch；tail_only独立user；并行工具先pending，下一request assistant calls→全部paired results→source。desired/applied ToolSet在安全边界切换。对应 context1.5/1.6剩余、4.6、5.1/5.2/5.4、6.7/7.1及itemized1.5/1.6生产证据；无固定交付前不勾整项。
2. **activation**：现有组件接到真实user/internal执行，唯一freeze owner；assembly_ref=ResourceIdentity，不用execution ID冒充turn，不造internal Turn。保真实资源激活快照、来源控制、sealed重试；复验编译reasoning-only合法body及耗尽收敛。
3. **delegate publication**：Session SQLite publication ledger与child发布同事务，真实factory/start caller与abort/recovery闭合，不倒删published，不造第二JSON账本。
4. **联邦 URI与target-main**：复用现有authority/grant/channel/cursor。已有 GET `/api/v1/sessions/{session_id}/main-thread` 是合同来源，不新增同义endpoint；稳定Gateway authority、受控peer-hub-star最多transit1/hops2/deadline，真实B⇄A⇄C与零副作用拒绝。证据 `remaining-addressing-contract-review/attempt-1/report.md`。
5. **持久资源与最终验证**：1.4/5.3外置owner unknown崩溃窗口后新进程重开，5.2唯一正式WebE2E仍待。只读reviewer正在固定1c20核现有harness，报告 `persistent-remaining-review/attempt-1/report.md`；实现仍交同Sol。
6. **继承消费者/关闭风险**：两个session_navigation旧DTO测试迁到durable operations/receipt，不恢复旧API；通用fixture10秒强杀Gateway父级的关闭风险仍待精确owner handle回收和透明失败，正常attempt7不能证明强杀安全。证据 `remote-cleanup-review/attempt-1/report.md`。最后逐原task/入口验收七change及handoff，不能当前阶段完成就结束goal。

## 实测改进与未证结论

- 固定交付、真实入口与增量早审已实际阻止残缺包落地；同树已过审查按路径/依赖复用，不重审未变全量。125+27path旧审保留；报告baseline转录更正不是源修复。
- importer首份audit曾漏目录创建后退出，首修又漏空/截断next。真实O_EXCL+部分写入+os._exit73的recover/direct retry四红；8958净增9生产行后固定64全文件通过（pytest79.44s、实际0/81.290s），完整/错误身份/未知残留/已发布损坏/恢复自身再次退出均核；规则效果已验证。
- reviewer恢复读错schema3 tree，误撤缺陷；纠正base/target/报告根后回到9bec真实caller/callee。计审查返工，不归Sol；后续效果继续观察。
- 主线程错误诊断“Gateway缺句柄”已撤销。固定7f8dc已有两handle，真实Bun退出3约155ms和SIGTERM均可见，无源码/测试增量。清旧库后服务恢复；此前等待为何未看到退出仍未证明。证据 `dev-launcher-early-exit/final-readonly-attestation.json`。不对反证已推翻的诊断派实现或写镜像测试。
- 主错误成本单列：不存在preload前置失败0.081s、首次依赖patch含已相同新增文件被拒（未写源码）、初始化探针猜错字段connection_state。原raw/exit保留，不计作者返工，不以重试覆盖。
- ROOT共享index当前观察98850fa4…；WT默认index e1cb50fd…。早前f2afa→98850字节变化，但ls-files --stage逐字相同（stage SHA2ae32540…），发起者未归因、未恢复共享index。每次集成核fresh现场，不能宣称全期字节未变。
- 比较数据只维护 `efficiency-comparison.json`；首完整候选、真实入口遗漏、返工、依赖等待、主补救分开计。没有同起点可比试验，不给速度倍率，不宣布永久最佳模型。当前一个Sol贯穿、独审、主串行集成继续；不把继承缺陷、主协调错误或过去Luna投入全归当前作者。

当前task勾选数仅用于恢复导航，不证明业务完成：

| change | 已勾 | 未勾 |
|---|---:|---:|
| add-context-injection-lifecycle | 21 | 36 |
| add-itemized-rollout-context | 50 | 37 |
| add-multi-workspace-backend-mounting | 5 | 29 |
| add-unified-virtual-resource-addressing | 7 | 34 |
| add-workspace-persistent-resource-management | 17 | 4 |
| migrate-identifiers-to-uuidv7 | 49 | 0 |
| migrate-session-context-resource-uri | 20 | 15 |

已关闭阶段查文档提交 `e972590dcd0bbe4122aae3bce870e7f72894ed65` 及上述原产物。本次压缩前台账快照保留于 `skill-state-maintenance/pre-compaction-68d597bf20f6.md`，其中“未集成”等记述已被本文当前状态替代；不作为现行指令，不复制回入口。
