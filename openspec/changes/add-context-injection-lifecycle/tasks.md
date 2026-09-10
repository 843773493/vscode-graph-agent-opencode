## 1. Context mutation、stable-prefix 与 source domain contract

- [ ] 1.1 定义统一 `ContextMutationIntent` union及其 `AppendCanonicalItemIntent`、`ApplySourceLifecycleDecision`、`SwitchToolSetIntent`、`RebuildContextEpochIntent` 分支；同时定义 thread-qualified `ContextSourceItem`、source/facet identity、revision、base/full/delta manifest与 tracking state，明确 CSM只负责 source lifecycle且各 intent保留自己的 domain owner、不变量、幂等键和 failure outcome。
- [ ] 1.2 定义 `prefix_epoch`、`epoch_reason`、parent assembly、stable prefix byte length/hash、Provider-profile item frame、精确 ToolSetRef/policy compatibility key和 appended item references；固定只有首次组装、实际 compaction重建、rewind重建和 ToolSet hard rebase能开启新 epoch，同一 Turn允许多个 model-call-scoped epoch。
- [ ] 1.3 固定 `skill_load(name, mode="snapshot" | "tracked" | "untrack" = "snapshot")` 工具 schema、结果 schema、名称解析及路径隐藏合同，并确认不存在其它 Skill 即时移除入口。
- [ ] 1.4 增加 mutation/source/stable-prefix/ToolSet compatibility schema与 hash golden vectors及错误合同，覆盖 Unicode、空内容、连续 revision、frame serialization、`not_tracked`、source conflict、非法 intent组合、ToolSet mismatch和 stable-prefix violation。

## 2. Storage、checkpoint 与 assembly owner

- [ ] 2.1 审计并补齐 `(session_id, thread_id)` owner、thread catalog locator、canonical mutation provenance、逻辑attachment/variant reference、source revision/detail、metadata/activation facet、tracking registration、prefix epoch/reason、assembly parent、stable prefix hash/length、desired/applied ToolSet revision、ToolSet/policy compatibility key和 selection provenance字段；不得新增注入计数或自动到期状态，不得把 `checkpoint_ns` 当 owner key，也不得把workspace blob物理路径写入canonical/source/detail。
- [ ] 2.2 为新增字段和 tracking control state 增加显式、版本化 SQLite migration，保证旧 JSONL bytes、item identity、assembly 和 history view 不被覆盖或隐式回填。
- [ ] 2.3 在每个 SessionThread 唯一的 RolloutCheckpointSaver/ContextStore owner增加四类 mutation intent端口，并让 CSM只使用 source observe/register/track/untrack/reconcile子端口；所有分支使用同一 read snapshot和 owner transaction，workspace runtime只负责组件装配，业务层不得旁路 RolloutStorage。
- [ ] 2.4 将 canonical append、source item/tracking/checkpoint state、ToolSet switch、active-view rebuild、plan和 assembly selection纳入原子提交边界；提交失败时不推进 item ordinal、committed revision、applied ToolSet revision、prefix epoch或 diff基准。
- [ ] 2.5 将同一 assembly的 canonical ordinal/tool配对、source identity/revision唯一性、base→delta顺序、plan ordinal、ToolSet compatibility和 stable parent-prefix校验接入 seal/dispatch preflight。
- [ ] 2.6 让 Skill snapshot/tracked 正文和 source diff 使用现有受保护 detail owner及 retention/GC；required detail 缺失阻止 seal，optional detail只能显式 omission/loss。
- [ ] 2.7 增加独立进程的 checkpoint/assembly恢复检查，覆盖 canonical append、tracked、untracked、pending candidate、desired/applied ToolSet revision和 sealed Provider frames，禁止从当前文件或当前 ToolSelectionStore重建旧 request。

## 3. ContextSourceManager 与 reconciliation

- [ ] 3.1 在 thread-qualified rollout context runtime owner中实现 source-specific `ContextSourceManager`，只消费结构化 observation、SkillCatalog descriptor和 Saver source端口并返回 lifecycle decision；不得截获 canonical append、ToolSet/compaction mutation，不直接读写 JSONL/SQLite、重组全量 history或构造 Provider message。
- [ ] 3.2 实现 snapshot 的一次稳定读取和不可变 ambient activation item；之后不注册文件检查，也不在 rewind 缺失时自动恢复。
- [ ] 3.3 实现 tracked registration 与 before-model 稳定读取，区分 observed、pending、committed 和 latest-visible-committed revision，以后者作为 diff 基准。
- [ ] 3.4 实现多个未提交变化的合并：只生成 latest-visible-committed→current 的一个 delta，保留 observation provenance但不创建可撤销的中间 context item。
- [ ] 3.5 实现 `untrack` 的 checkpoint-versioned tracked→untracked/frozen 转换；停止 stat/hash/diff 和 rewind 自动恢复，不追加、删除或改写 Skill source正文，非 tracked 情况返回 `not_tracked`。
- [ ] 3.6 实现 rewind/compaction reconciliation：按目标 checkpoint恢复 tracking state；tracked 缺失时追加当前完整 revision，snapshot/untracked 不恢复，物化和恢复 item均标记为 post-user user-role source。
- [ ] 3.7 让重复 tool call、observation、prepare、seal、model attempt 和 transport retry复用稳定 candidate/assembly/frame；revision、hash 或 item bytes 冲突时 fail closed。

## 4. SkillCatalog 与完整 producer 迁移闭包

- [ ] 4.1 建立 bundled、`${BOXTEAM_HOME}/skills`、`${workspace_abs_path}/.boxteam/skills` 三层 SkillCatalog，按 `workspace > gateway-global > bundled` 解析唯一 entry；Gateway只管理全局 catalog，不写工作区 Session状态。
- [ ] 4.2 解析 Skill metadata 时只接受 `name` 和 `description`，将 metadata 与 activation 建立不同 source facet identity；模型可见 catalog和诊断不得泄露内部路径；移除 `allowed_tools` 对 Skill context contract 的参与，若工具事件仍需 skill_names 归因则迁到独立工具归属配置。
- [ ] 4.3 注册 `skill_load` 工具并让模型只传名称/mode；工具结果只返回安全的名称、mode、revision/hash 标识和 append 状态，不返回路径或完整正文。
- [ ] 4.4 修改 Skills middleware，删除依赖通用 read 工具完成 Skill activation 的路径；普通文件读取 `SKILL.md` 不注册 activation/tracking，也不得重复发送正文。
- [ ] 4.5 重构 `WorkspaceAgentsMiddleware`：首次 root contribution、before-model 文件变化和 compaction marker只产生 source observation，不再直接追加未登记的 `HumanMessage`，也不增加 watcher/`rg` 扫盘。
- [ ] 4.6 迁移设计表 R01–R07/R09初始 instruction producer：Agent配置说明、runtime identity、条件化团队规则、Todo、Skill metadata、Filesystem、compact tool说明和显式 memory分别注册 source provenance；条件化 root绑定精确 ToolSet policy并只在合法 hard rebase epoch重编译，对应 tool descriptions只进入 ToolSet，默认未接线 memory不得伪装启用。
- [ ] 4.7 迁移设计表 E01–E07 runtime event producer：Goal、delegated/generated result、非模拟用户 session message、team membership/task、terminal completion、四类 execution retry和各类 checkpoint reminder先提交 ambient/pending source，再走不创建 user root 的幂等 execution wakeup；generated seed显式区分 user-derived root与 internal source。
- [ ] 4.8 删除 checkpoint直接 message mutation、`PromptReplayCaptureMiddleware`、捕获标签/instrument链及 prompt diff/synthetic assembled prompt路径；增加 AST/import、source-registry completeness与 sealed manifest/dispatch hash正向对账断言，禁止未登记 `HumanMessage`、普通 `MessageRole.user` Turn或通用 read激活内部 source，同时保留旧历史读取。

## 5. ToolSet hard rebase、Provider、history 与 display projection

- [ ] 5.1 修改 plan compiler，使首次组装时把设计表中所有已启用 R类初始贡献编译为一个不可变 root system item并保存每个 source的 identity、revision、ordinal、hash、included reason、ToolSet binding、visibility和 loss；只在 compaction、rewind或 ToolSet hard rebase的新 epoch重编译 root，且不吸收 post-user source。
- [ ] 5.2 修改 LangChain/native Provider projector，使第一条真实用户消息后的 CSM full、delta、rewind restore 和 compaction materialization均输出独立 user-role item，不合并进旧 system/user item、不前插也不按 role 重排。
- [ ] 5.3 实现 Provider-profile item frame封存、ToolSetRef/policy compatibility key及父 assembly逐字节前缀校验；同一 sealed assembly重试必须复用 frames/tools，adapter自动合并/规范化或同 epoch ToolSet漂移时显式 reject。
- [ ] 5.4 保持 ToolSelectionStore/ToolService拥有 workspace/agent级 desired revision、每个 SessionThread ContextStore拥有 observed desired/applied binding，并在每次 model call前比较二者和实现 safe-boundary hard rebase：in-flight sealed call不可变、旧 outstanding call先产生真实配对 terminal outcome、快速切换可合并到最终 desired、应用后创建 `toolset_changed` epoch且同 Turn可多 epoch；Provider无法投影旧工具历史时 fail closed。Provider capability matrix只保留关闭状态的 Anthropic中途 system TODO，并保持原生 tool call/result role。
- [ ] 5.5 废弃 `ItemizedContextProjectionMiddleware` 的 prompt/tool/context反向捕获、prepare/seal、状态与 fallback职责；框架确需 model-call hook时实现无状态 sealed-assembly dispatch bridge并置于最后一个 request-mutating位置，否则删除该 middleware，由 Provider adapter直接消费 Saver-issued dispatch reference；history/diagnostic独立读取同一 sealed selection。
- [ ] 5.6 分离设计表 D01/C01–C03：派生摘要请求使用独立 sealed assembly且 summary保留 compaction owner；真实用户输入/附件及 assistant/reasoning/tool协议事实通过 canonical append intent提交，ToolSet通过 switch intent提交，它们共用 ContextStore owner但不进入 CSM；附件正文只由workspace content-addressed catalog按capability和thread/item/view membership读取，模型与历史不接收物理locator，展示文本不得反写任何 canonical/source/ToolSet/control状态。

## 6. Rewind、compaction、restart 与边界测试

- [ ] 6.1 增加跨多次正常 model request的 stable-prefix byte golden test，覆盖 root system、用户/assistant/tool canonical append、full source、delta、tool loop、重复 dispatch和 adapter禁止合并；验证普通 append保持 epoch，而 ToolSet变化必须 hard rebase并更新 compatibility key。
- [ ] 6.2 增加 Skill snapshot/tracked/untrack 状态机测试，覆盖默认 snapshot、无变化零追加、A→B→C 未提交合并、`not_tracked`、重新 tracked及路径不泄露。
- [ ] 6.3 增加 rewind checkpoint矩阵，验证 snapshot移除不恢复、tracked registration保留时以 user role恢复完整 revision、untrack 前后恢复对应 checkpoint状态。
- [ ] 6.4 增加 compaction materialization测试，验证动态 source完整恢复仍为 post-user user item，旧链可审计且不重复投影，snapshot/untracked不重读文件。
- [ ] 6.5 增加进程重启、GraphBinding/ThreadRuntimeBinding与main/direct及non-main/date-shard locator解析、workspace attachment去重/ref-release/tombstone/GC/capability、detail GC/forbidden、revision conflict、SQLite transaction failure、未提交 candidate及 pending desired ToolSet测试，确认禁止扫盘/物理路径泄露、fail closed、幂等、原子回滚与旧 assembly bytes/tools不变。
- [ ] 6.6 增加 bundled/Gateway-global/workspace SkillCatalog集成测试，验证固定优先级、所有工作区默认可用、metadata只含 name/description及 Gateway不写 Session存储。
- [ ] 6.7 为设计表 R01–R09、E01–E07、D01、C01–C03、P01建立逐项 producer/domain-owner/mutation-intent覆盖矩阵；增加 ToolSet add/remove/visibility/policy hard rebase、outstanding call收敛、同 Turn多 epoch、Provider旧工具历史不兼容，以及 PromptReplay零残留、bridge无状态、reference错配、retry同 bytes/tools、bridge后 mutation拒绝和 LangChain/native/history一致性测试；未登记生产 prompt差异必须 fail closed。

## 7. 产品回归与交付门槛

- [ ] 7.1 更新 Agent factory/deep stack/Skill/AGENTS/compaction、Goal/subagent/session messaging/team/terminal/retry/checkpoint、ToolSelectionStore/ToolService/execution step/ToolSet registry、rollout context integration、sealed dispatch bridge/Provider projection和 recovery fixture，删除 PromptReplay测试与旧反向捕获预期，禁止恢复旧 v1 fallback、直接内部 message注入、Turn开始后永远沿用旧 Agent工具集或第二 checkpoint writer。
- [ ] 7.2 运行修改 Python 文件对应的 Ruff、compileall和 targeted pytest，并保留退出码、测试计数和失败诊断；未验证项不得勾选。
- [ ] 7.3 运行 `uv run pytest tests/e2e/clients/web/test_basic_chat_tool_loop.py`，确认新增 user-role source 不重复工具/思考/summary item，也不改变现有 Web tool-loop语义。
- [ ] 7.4 对真实 Provider request log、封存 frames/tools、stable-prefix epoch/reason/manifest、desired/applied ToolSet revision、checkpoint state、assembly selection和 history projection做端到端 provenance audit。
- [ ] 7.5 运行 OpenSpec strict validation、44 项任务计数、git diff/status和架构/import audit；确认本 change与 `add-itemized-rollout-context` 的 SessionThread/GraphBinding 边界清晰后才进入 apply/implementation。
