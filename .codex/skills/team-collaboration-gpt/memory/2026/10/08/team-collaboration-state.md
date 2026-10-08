# 当前协作状态（2026-10-08）

只维护当前范围与恢复入口；模型和决策规则见 [技能入口](../../../../SKILL.md)，路径、Git与派单见 [共同流程](team-collaboration-workflow.md)。原 goal 仍 active，未完成；不能因一批源码或 Luna 小修交付而结束。

## 坐标与唯一作者

- ROOT=`/data1/hyf/20260822_agent/vscode-graph-agent-opencode`，旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。TEMP=ROOT/out/tests/temp/2026/10/04/024121-team-execution；A=TEMP/coordinator/artifacts/sol-efficiency-trial。派单必须展开完整绝对路径。
- 当前 HEAD、租约、固定收据、实际任务与观察 cursor 查 `A/current-recovery-receipt.json`。该入口只保当前事实；历史原始台账位于其 `historical_receipts`，按任务键读取，不全文恢复。文档不追逐自身提交坐标。
- 同一个 `/root/sol_vertical_implementer`（已核 `gpt-6.1-sol/max`）独占 ROOT 核心源码和受保护测试；主代理仅维护规范、技能、AGENTS、固定交付与串行集成，不并发手写业务源码。
- `gpt-6-luna/max` 只做固定候选独审或隔离且有实证的小修。旧失败 agent 不复活凑人数；实际模型不能用 follow-up 切换。保护面、索引、绝对产物根以本批租约为准。
- 正式测试沿 `out/tests/<同名测试路径>/`，全部带进程外保护。不 `/tmp`、整仓 copy、新 venv、源码根注册、旧临时业务工作区复用。保 SSH 凭据、普通源码、未集成独有改动和 demo；旧开发中间数据不兼容，但不能删 fresh 数据掩盖 writer 缺陷。官方开发全组保持停止，不启动 WIP 产品服务。

## 已接受与本批

- A01–A03、UUIDv7与persistent-resource的具名收据保留在历史台账；按实际依赖复用，不因任务未勾推定缺代码，不将窄验收扩为整个 change。
- **A04 原授权源码闭包已接受**：105源码＋11主文档完整 tree `8215be15286152bcb2514ff7568507c1fde457b6` 集成 `c6c7b7af745316cccb89bb6b6d9f498e4cf0d9b8`。机械重建、两固定主题独审、guard/双祖先/共享 index 检查通过。入口 `A/addressing-existing-source-bulk-dispatch/ROOT-full-final-candidate/`。新 files/Skill 目录根、child 编码和统一文件工具仍仅规划，未授权源码，不据此归档整个 change。
- **A05 完整闭包已验收，固定集成中**：唯一 managed Backend handle/generation/完整不可变 mount-set；全组 drain/force/rebuild/close/adopt/recovery、Gateway proof/CAS和配置补偿；per-mount Terminal/Browser及配置候选启动输入；DTO全 caller迁移、旧聚合物理删除；MessageStream纯投影与原 Store 事务边界；Catalog真实COMMIT失败清理。租约 `A/mounting-lifecycle-dto-source-bulk-dispatch/root-single-author-lease.json`，产物 `TEMP/sol_vertical_implementer/artifacts/mounting-lifecycle-dto-source-bulk-core-and-boundaries/`。
- A05基线为c6，作者观察index由主初始化该对象；之后仅主文档提交合法漂移，不强迫重建first preimage。一次迁齐生产/caller/协议/Web和核心fixture，少量真实双mount核心后集中边界；最终整批停写再冻结，不微包或逐路径请示。真实核心必须一个Gateway/一个managed Backend/两个mount，双Backend用例不能替代。
- A05最新动态收据已独核至run039：Gateway完整unit621、受影响Web123、真实Terminal停止、真实页面完整组生命周期及双mount业务隔离通过；run038请求/恢复22项和run039严格类型实际0。真实页面为受控Provider Integration，不称产品E2E。CAS旧引用、页面自身SSE/DTO持久writer及compiler外部传输fixture的失败原始输出保留；批末一次Webbuild已验。scoped Ruff156→142、新增0，不称全绿。固定候选独审及最终组合真实三核心是否重验仍按当前收据核验，未完成前不接受A05。
- A05停写完整候选已机械重建：162源码＋4主文档，原tree `b97def0d12666845a080077d1d1821b8cfa89744`，仅EOF/技能修订tree `d0f7ad04dedb5ad8eb6af283a4396a2b05ef2f55`；7个精确defer保留，保护SHA未变。Wire固定独审发现A05-L1 P1：最后成员DELETE直接close、且合法空desired被schema/发起成员校验挡住，绕过完整组drain及force durable；同Sol正在修唯一owner及全部caller，不交Luna核心改。Storage/isolation最终报告补核A05-C1 P2：rollback自身失败后store仍暴露事务连接；同Luna在固定d0独立Git worktree做Catalog两文件窄修，Sol明确保留这两路径，主独审后给固定补丁再跑最终组合。两报告均无其它finding。三真实核心最终组合未重验，已让Sol在本次完整修订后一次补验；原run012只作历史绿。入口 `A/mounting-lifecycle-dto-source-bulk-dispatch/ROOT-full-final-candidate/`，独审报告 `TEMP/wire_contract_reviewer/artifacts/a05-fixed-owner-contract-review/`、`TEMP/luna_narrow_fixes/artifacts/a05-fixed-storage-isolation-review/`，窄修租约 `TEMP/luna_narrow_fixes/artifacts/a05-catalog-rollback-isolation/lease.json`。
- Luna用户三项实证修复已集成 `7d7178ef`（事务清理、正文深隔离、联邦异常分类）；后续Inspector send/receive/primitive异常修复已集成 `3fac7072`/`95ab847b`/`9711d5a8`。保护精确源码/独立测试列在A05租约。已集成代码副本清理，raw/报告保留。
- C1窄修固定tree `f136a8665abd92cc0542f02610872db43305088f`（d0＋Catalog两文件）已主独审、严格重建且作者额外源码0，并以固定blob合ROOT，未单独commit；供同Sol最终3真实核心。Ruff0，作者10pass selector漏新generic失败node，主精确补4pass，合14相关用例。收据 `A/mounting-lifecycle-dto-source-bulk-dispatch/catalog-rollback-narrow-fixed/root-fixed-blob-compose-receipt.json`。窄修code worktree保留到完整集成再清，未登记为业务工作区；原事务顺序/锁不变，不扩大到全库大改。
- A05-C2同类窄查已复现SessionControl rollback失败未隔离、公共连接仍可见未提交数据；原测试手工rollback掩盖。主已核固定f136方法探针actual0/raw SHA，授权同Luna在现存Catalog代码worktree仅改SessionControl源码/独立事务测试，保先前Catalog停写两文件，不写ROOT；租约 `TEMP/luna_narrow_fixes/artifacts/a05-session-control-rollback-isolation/lease.json`。指定完整文件4pass、目标Ruff0、主AST独审仅write CM变化；固定tree `d825dedfcbafb03730eb3d56d3b2f706c488a9f3`严格重建且作者额外源码0，已接收窄依赖。Sol正式停写后已将两固定blob合ROOT，正常事务依赖不变、不重跑其已验核心。目录Ruff另有未改I001，不能称全绿。
- A05-L1修订run002 actual1：95pass/3fail，主已核raw/SHA；两例旧generation恢复重分配辅助端口、一例新fixture缺service_urls。同Sol修完整旧manifest恢复，不放宽原比较；最终组合三核心与固定增审仍待停写。
- Luna两项Node只读调查无新finding：真实primitive响应复用，request timeout/cancellation/receiver路径无需新探针、测试或分支。报告 `TEMP/luna_narrow_fixes/artifacts/node-remote-value-readonly-probe/`、`node-request-lifecycle-readonly-probe/`。不为凑人数制造探针；Wire待最终固定增量，不能以Luna自审代替独立审查。
- 退役目录已清两批：首批6个真实run-*数据叶＋7个旧Web dist约1.62GiB，第二批4个正式镜像workspace约269.9MiB，删除前合计约1.88GiB；正式artifacts、补丁和独有源码保留。3个旧E2E孤儿服务的pidfd SIGTERM退出已核，原报告误列测试父目录及无证据进程观察已撤回、原版保留。主收据 `A/retired-directory-cleanup/delete-receipt.json`、`workspace-delete-receipt.json`；后续Wire正确列叶且披露未核进程，但遍历/宽搜耗时仍高。技能改为已知候选或tests路径映射，效果待下一相关派单。

## 下一批与证据纠正

- **A06已准备、未派发**：同Sol继续所有原剩余Thread准入/child生命周期、residency/GraphBinding、右侧child UI、可恢复后台任务/持久mailbox、pending父Folder两处UI，以及原导航和最终验收。租约 `A/final-original-scope-source-bulk-dispatch/root-single-author-lease.json` 已逐项认领业务owner，不只记为“最终验收”；待实际A05完整基线后派发。37 mixin和平行状态表无业务复现，不交Luna大改。
- session-uri 5.2原零存量raw未定位：按用户旧开发数据可删/无兼容授权修订proposal/D5/spec/tasks为明确数据政策，保历史摘要及未复核限制，不新扫描/迁移、不宣称历史取证通过，fresh writer/reader/恢复要求保留。9.1/9.2共owner和冻结目标已接通，无确认源码finding；三入口bare/full URI、full URI wait及切换/重连目标冻结的联合收据仍待A06，保持未勾。报告 `TEMP/wire_contract_reviewer/artifacts/a04-final-three-task-evidence/`，规范修订 `A/session-uri-historical-policy-reconciliation/`，strict实际0。
- 原文件树“无瓶颈”登记误把目录GET median16.26ms写成mutation p95，并将源码推断的重载/无丢失更新写成实测。主已撤回该结论；10.4b/10.7证据不全，10.4c未开工。A06补真实mutation p95/运行重载/FS读取/并发更新证据后按原阈值判；Gateway10.5须自身10.1量化，不用Session/GET数据代替。报告 `TEMP/wire_contract_reviewer/artifacts/a06-navigation-conditional-evidence/`。只证明证据纠正与队列安排，后续量化效果未验。

## 限制与技能效果

原真实Provider403 quota和补充C1外部registry端口前置仍UNMET，不重复请求或改环境求绿。A04原100条Ruff诊断无新增，不称全绿；三项Buf breaking为授权破坏且tag未复用。Buf完整暂存后替换的失败合同已验，后续SSE阶段/磁盘copy异常原子性未认证。TTL允许detail不可读或exact replay失败，不承诺永久正文读取。

技能已依据实际漏caller、残缺补丁、重复微包/检查、跨主题重复独审及量化误记修订，详见 [条件性细则](delivery-failure-rules.md)。具名后续结果才记有效；没有同起点Sol/Luna A/B，不宣布速度倍率。比较入口 `A/efficiency-comparison.json` 同样只保当前批与历史查询索引；主补救、继承投入与作者返工分开。旧完整状态已保存在 `A/receipt-history/team-collaboration-state-pre-thin-routing-20261008.md`，只按具体问题查，不当当前权威。

- A05-L1及C1/C2组合已停写并机械冻结为 `785919ada5a83f65cdbd565218424ecd87966dad`：168源码＋7主文档，166作者源码/39增量/13保护SHA/17唯一收据已主核，完整diff-check与严格重建实际0。Gateway643为最后HTTPError catch前结果，该增量有最终7精准例；指定新真实退出2、真实Web1及批末build/strict通过。原三核心在overall1中的个别3pass按未变依赖复用，不伪称原整文件全绿。固定入口 `A/mounting-lifecycle-dto-source-bulk-dispatch/ROOT-full-final-candidate/revision2/`。
- 最终增审新增 **A05-L2/L3 两项P2**：报告 `TEMP/wire_contract_reviewer/artifacts/a05-fixed-controlled-exit-revision2-review/`。L3两文件固定tree `2c4ab256977e2557afa5d70aed94181b857f3bb0`严格重建/作者额外源码0，Wire窄独审无finding，已以固定blob合ROOT；一唯一真实路由故障/重试用例三次actual0，最终新test Ruff0、route忽略既存27项B008后0，主固定拷贝静态0。入口 `A/mounting-lifecycle-dto-source-bulk-dispatch/remote-forward-narrow-fixed/`，独审 `TEMP/wire_contract_reviewer/artifacts/a05-remote-forward-narrow-fixed-review/`。Luna停写，无新finding不制造任务；代码副本待源码集成再清。
- **A05-L2/L3最终增审通过，源码固定集成中**：L2四文件与必要caller固定独审无finding，报告 `TEMP/wire_contract_reviewer/artifacts/a05-stopped-member-exit-revision3-review/`；169源码＋7主文档tree `8327024da83027bde68b89f709ac36ca41d5d7fa`严格重建通过，7精确defer及保护SHA不变。35核心＋21边界＋1真实Gatewayactual0，L3一唯一失败/重试用例actual0；依赖未变动态、旧主题与Webbuild复用。9.2/9.3与第10节已验收，不勾整个change。A06在实际集成后派同Sol；Luna仅只读核附件事务异常的新候选，未获得ROOT源码写权限。整个goal仍active。
