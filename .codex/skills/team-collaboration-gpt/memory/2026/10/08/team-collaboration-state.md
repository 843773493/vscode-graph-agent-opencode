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
- **A05 正在同 Sol 整批实施，未验收**：唯一 managed Backend handle/generation/完整不可变 mount-set；全组 drain/force/rebuild/close/adopt/recovery、Gateway proof/CAS和配置补偿；per-mount Terminal/Browser及配置候选启动输入；DTO全 caller迁移、旧聚合物理删除；MessageStream纯投影与原 Store 事务边界；Catalog真实COMMIT失败清理。租约 `A/mounting-lifecycle-dto-source-bulk-dispatch/root-single-author-lease.json`，产物 `TEMP/sol_vertical_implementer/artifacts/mounting-lifecycle-dto-source-bulk-core-and-boundaries/`。
- A05基线为c6，作者观察index由主初始化该对象；之后仅主文档提交合法漂移，不强迫重建first preimage。一次迁齐生产/caller/协议/Web和核心fixture，少量真实双mount核心后集中边界；最终整批停写再冻结，不微包或逐路径请示。真实核心必须一个Gateway/一个managed Backend/两个mount，双Backend用例不能替代。
- Luna用户三项实证修复已集成 `7d7178ef`（事务清理、正文深隔离、联邦异常分类）；后续Inspector send/receive/primitive异常修复已集成 `3fac7072`/`95ab847b`/`9711d5a8`。保护精确源码/独立测试列在A05租约。已集成代码副本清理，raw/报告保留。
- Luna两项Node只读调查无新finding：真实primitive响应复用，request timeout/cancellation/receiver路径无需新探针、测试或分支。报告 `TEMP/luna_narrow_fixes/artifacts/node-remote-value-readonly-probe/`、`node-request-lifecycle-readonly-probe/`。两审查者等待A05固定候选，按主题分主审、不读Sol live。
- 退役目录首批已清：6个真实run-*数据叶＋7个旧Web dist，删除前合计约1.62GiB；正式artifacts、补丁和独有源码保留。3个旧E2E孤儿服务的pidfd SIGTERM退出已核，原报告误列测试父目录及无证据进程观察已撤回、原版保留。主清理收据 `A/retired-directory-cleanup/delete-receipt.json`，更正报告 `TEMP/luna_narrow_fixes/artifacts/retired-runtime-directory-census/`；仅证明本批纠错与删除，后续派单规则效果仍待验。

## 下一批与证据纠正

- **A06已准备、未派发**：同Sol继续所有原剩余Thread准入/child生命周期、residency/GraphBinding、右侧child UI、可恢复后台任务/持久mailbox、pending父Folder两处UI，以及原导航和最终验收。租约 `A/final-original-scope-source-bulk-dispatch/root-single-author-lease.json` 已逐项认领业务owner，不只记为“最终验收”；待实际A05完整基线后派发。37 mixin和平行状态表无业务复现，不交Luna大改。
- session-uri 5.2原零存量raw未定位：按用户旧开发数据可删/无兼容授权修订proposal/D5/spec/tasks为明确数据政策，保历史摘要及未复核限制，不新扫描/迁移、不宣称历史取证通过，fresh writer/reader/恢复要求保留。9.1/9.2共owner和冻结目标已接通，无确认源码finding；三入口bare/full URI、full URI wait及切换/重连目标冻结的联合收据仍待A06，保持未勾。报告 `TEMP/wire_contract_reviewer/artifacts/a04-final-three-task-evidence/`，规范修订 `A/session-uri-historical-policy-reconciliation/`，strict实际0。
- 原文件树“无瓶颈”登记误把目录GET median16.26ms写成mutation p95，并将源码推断的重载/无丢失更新写成实测。主已撤回该结论；10.4b/10.7证据不全，10.4c未开工。A06补真实mutation p95/运行重载/FS读取/并发更新证据后按原阈值判；Gateway10.5须自身10.1量化，不用Session/GET数据代替。报告 `TEMP/wire_contract_reviewer/artifacts/a06-navigation-conditional-evidence/`。只证明证据纠正与队列安排，后续量化效果未验。

## 限制与技能效果

原真实Provider403 quota和补充C1外部registry端口前置仍UNMET，不重复请求或改环境求绿。A04原100条Ruff诊断无新增，不称全绿；三项Buf breaking为授权破坏且tag未复用。Buf完整暂存后替换的失败合同已验，后续SSE阶段/磁盘copy异常原子性未认证。TTL允许detail不可读或exact replay失败，不承诺永久正文读取。

技能已依据实际漏caller、残缺补丁、重复微包/检查、跨主题重复独审及量化误记修订，详见 [条件性细则](delivery-failure-rules.md)。具名后续结果才记有效；没有同起点Sol/Luna A/B，不宣布速度倍率。比较入口 `A/efficiency-comparison.json` 同样只保当前批与历史查询索引；主补救、继承投入与作者返工分开。旧完整状态已保存在 `A/receipt-history/team-collaboration-state-pre-thin-routing-20261008.md`，只按具体问题查，不当当前权威。
