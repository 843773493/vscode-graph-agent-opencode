# 交接：边界停下（bug 猎捕 + 冗余收敛）

日期：2026-10-03
停止点 HEAD：`11dcfa394261e70ffa7aba3a033a285eaebb1bb2`
工作树状态：**当时完全干净**（独立索引 `read-tree HEAD` 后 `git status` 零残留）。这是历史停止点，恢复任务需重新检查。

2026-10-03 调度整理：handoff 文档提交为 `9bc4246b`；模型、派单、路径和提交方法改由 [团队协作技能](../../.codex/skills/team-collaboration-gpt/memory/2026/10/10/team-collaboration-workflow.md) 维护。两种模型版本已准备，本轮仅整理文档及遗留目录，未继续 OpenSpec。目录问题与待审建议见 [目录审查](20261003-151500-team-collaboration-directory-review.md)。

2026-10-04 迁移更新：仓库物理根为 `/data1/hyf/20260822_agent/vscode-graph-agent-opencode`，原路径为软链接；后续不复用历史临时测试工作区。默认工作区与 `/data1/hyf/test_workspace/drive_bicicle` 的旧 `.boxteam/` 数据已分别按用户授权清理，普通文件保留，两个连接均恢复 `ready`。完整开发服务已恢复（前端 8027、Gateway 8030）。以上为迁移时的历史服务状态，恢复时需重新核验。当前 goal 已继续本交接及七个关联 OpenSpec 的实施与独立验收，实际进展以技能实时台账为准。 2026-10-09 最新状态：原核心最终批及查询003/恢复fixture004已通过具名验收并串行集成`e11c8c6b`。导航006的fixture布局修订经独立017完成300文件样本，首双页并发因观察器等待本页POST而超时；共享队列由另一页派发，同operation ID已complete且两文件存在，Gateway样本0，完整300/40/20量化仍未通过。Luna只核跨页测试观察器窄修及已接受任务证据，Sol独占ROOT完成10.3真实失败对账、权威目录刷新与本地pending投影闭包。2026-10-10 增量：Sol013失败收敛及持久清理重试经固定增审后已集成`faad271c`，§10.3原合取要求已完成；导航024独立minimal在异步响应配对处actual1/39.56s，未启动full；Sol025接手完整观察器闭包及代表入口调试，Luna随后独验。最终原goal、Provider403/C1验收仍未完成，实际租约与收据见技能实时台账。用户随后授权按架构熵减自主决策，开发生成的新旧中间数据可直接删除，不保留旧兼容，不再逐项询问。

---

## 一、本轮边界说明

用户要求「找个边界停下，留下 handoff 文档」。本轮的完整边界是：

- 最后一批中断 agent 的在途工作已由 owner 收口、验证并落成 `11dcfa39`；
- 工作树零残留（垃圾探针已清理）；
- `import app.main` / `import app.gateway.main` EXIT=0；
- openspec validate = 40 passed / 0 failed；
- 未完成的工作全部转为下文「登记项」，不留在工作树里。

所有被复用的 subagent 均为 `newapi-local/deepseek-v4.1-flash`。

---

## 二、本提交会话累计落地（HEAD 附近 61 笔，新→旧）

| hash | 内容 |
|---|---|
| `11dcfa39` | 重构(测试与前端): 收敛重复样板（契约 openapi 快照 / 集成 api helper / LLM 日志失败分支 / provider 块类型常量）并补齐 cloneMaps 的 Set 克隆 |
| `803fe581` | 修复(scripts): 会话目录迁移报告层不再用 parents 向上推导 sessions 根 |
| `d2e1415d` | 修复(infra): 会话消息幂等索引读改写丢失并发写入 |
| `8e5233d5` | 清理 core 层两处未接线死代码（TurnExecutionScope lease 子系统、_COLLABORATION_MEMBER_STATES） |
| `d493060a` | 重构(gateway): 删除 registry 与用户 profile 两处零引用死方法（净减 41 行） |
| `a0263a90` | 修复(gateway): 补上 d5f5bc9a 改契约后漏同步的 openapi 摘要基线 |
| `f0711273` | 前向恢复(编排): 还原被本面提交误吞的 owner thread 工厂测试 docstring |
| `10f432b1` | 精简(api): 删除零引用的 CanonicalThreadId 复用类型 |
| `39b623b8` | 文档(编排): 澄清 owner thread 工厂测试的淘汰回收语义 |
| `144e5c29` | 修复(api): 会话列表 limit 补参数层边界，非法分页与同族入口同归 422 |
| `6456758c` | 修复(gateway): 会话目录搜索的三个按工作区缓存随删除回收 |
| `fe38591f` | 修复(itemized): naive 时间戳默认值 + plan_hash 顺序键去重（净减 19 行） |
| `d8667acc` | 修复(编排): OwnerThreadCreationFactory 服务缓存无界字典 → LRU 有界（并修 fd 泄漏） |
| `a4e9f7d7` | 修复(web): 运行动作迟到回写加会话守卫（4 条链路） |
| `88f57125` | 修复(trace): 会话唤醒 Condition 表 → 固定分片池 |
| `cbe33d4b` | 修复(context): 游标 offset 越界 fail-closed |
| `f4041baa` | 修复(资源账本): resources.json 损坏 fail-closed |
| `77af33df` | 前向恢复(trace): 回退误吞并发在途改动的 cbc15fc3 |
| `cbc15fc3` | 修复(trace): 录音器/存储会话锁表与 job 映射有界化（**该笔为事故源，见下**） |
| `64273559` | 修复(core): per-key 锁表抽 KeyLockPool 固定分片池（三处收敛） |
| `781ef751` | 修复(事件总线): publish 锁表改固定分片池 |
| `f5cb8e67` | 修复(session): InterruptState 全默认态不落键 |
| `d5f5bc9a` | 修复(gateway): 工作区列表端点补本地凭据 fail-closed |
| `46f2769d` | 修复(web): 后台连接控制失败改可见诊断并加会话守卫 |
| `edc42c29` | 修复(gateway): 运行时控制器锁表 → 固定 64 分片池 |
| `1d4e6445` | 修复(消息流): SSE 游标越界 fail-closed |
| `340a81fa` | 修复(api): PATCH folder 错误分类与同族入口一致 |
| `bfd4b78b`/`47168b4e` | 工具(git): 新增并改进隔离索引提交防线 |
| `e4f40ad1` | 文档(agents): 提交流程写入隔离索引防线 + 三次真实事故 |

更早批次见 `git log`（导航分页、SSE 心跳、Composer 边界、主题降级等）。

---

## 三、本轮核心主题：per-session / per-key 无界字典缓存与锁表（已 10 次）

这是本仓今日最高产的真实缺陷族。**统一修法**：固定 64 分片池 + `zlib.crc32(key) % 64` 取模（**不用 `hash()`**，保证跨进程确定），同一 key 恒命中同一把锁/同一分片、互斥语义不变、数量恒定 64。

已修（新→旧）：

1. `781ef751` job_event_bus publish 锁表
2. `f5cb8e67` SessionInterruptState `_states`
3. `88f57125` TraceEventStore `_conditions`
4. `cbc15fc3` trace_event_recorder `_session_locks`/`_job_sessions`、trace_event_store `_append_locks`/`_file_locks`
5. `64273559` app/core `_key_lock` ×3 → 共享 `app/core/key_lock_pool.py`
6. `edc42c29` GatewayWorkspaceRuntimeController `_locks`
7. `e18e4c48` message_stream_store `_index_locks`
8. `bdd6ecd1` SessionTurnReplayService `_session_locks`
9. `eade5389` SessionGoalService `_locks`
10. `d8667acc` OwnerThreadCreationFactory `_services`（LRU 上界，并修 fd 泄漏）
11. `6456758c` GatewaySessionCatalogSearchService 三个按工作区缓存

**变异验证口径（重要）**：有界性用例**刻意不 import 分片常量**，让还原无界实现后的变异直接命中被测行为（`assert N <= 64`）而不是 `AttributeError`。

### 原登记项的当前 owner 状态

| 位置 | 现状 | 判定 |
|---|---|---|
| `app/core/background_message_bus.py:18 _messages` | B01/B02 已集成 `28359675` | 删除排空后回收目标 Session 全部 backlog，活跃 collector 明确拒绝关闭；删除失败后 catalog `deleting` 拒绝消息准备、Job 创建和迟到 continuation。74 项相关单测及 1 项真实集成通过，两个入口单点变异分别命中拒绝断言，R19 独审通过。其它非 Job Session 写入准入缺口由 A20 单独继续收敛，不把 backlog 改成 LRU |
| `app/services/orchestration/thread_residency.py ThreadResidencyTracker._states` | 原缺口已由 owner 收敛 | 当前冷读不登记 slot；成功释放后 `_drop_cold_state` 回收，失败保 owner 重试，generation 不复用。固定 `9e7013e` 增量独审通过；通用强杀关闭风险仍另验，不重做 LRU。证据见 `out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/residency-stable-pre-review/final-report.md` |
| `app/services/infrastructure/trace_event_store.py _event_ids` 的自动 `evt_*` 条目 | 旧报告候选 | A13 已核验当前 Store/writer 使用调用方 Event ID，源码已无该侧车；不重开修复，证据见实时台账引用的 residency/Trace owner 审查 |

---

## 四、必须交由环境 owner 处理的两项（超出本仓库改动范围）

### 1. 共享 `.git/index` 陈旧（**最高优先级**）

实测状态：

- 索引条目 **3266**，而 HEAD 树条目 **3434**；
- 两份前端 openapi 快照在索引中仍停在旧 blob `eca0c3b7…`，而 HEAD 与工作树均已正确（`586d73ca…`）；
- 使用这份索引的 `git status` / `git diff HEAD` 会把未改动文件报成 `D`/`MM`；不能据此认定磁盘文件被删。这是索引与 HEAD 不一致，不是已证实的对象数据库损坏。

**危害**：任何不带 `GIT_INDEX_FILE` 的 `git add`/`git commit` 都会吞并陈旧暂存内容。本轮已真实发生 3 次「陈旧索引快照吞并发提交」事故：

- `bb3f3812` 吞掉 `90130062` → 由 `9b517333` 前向恢复；
- `cbc15fc3` 漏写 `GIT_INDEX_FILE=` 前缀，误用共享索引提交 **701 个并发路径**（含删除 `app/gateway/routes/workspaces.py`、回退两份 openapi 快照）→ 由 `77af33df` 前向恢复，**零内容丢失**（已独立核验）；
- `3ea48090` 用旧 read-tree 快照反向还原 `1287390f` → 由 `911491c2` 前向恢复；
- 另有 `f0711273` 同类自查恢复。

**当前处理**：保留共享暂存内容，不直接重建共享索引。状态检查使用任务自己的新索引；是否备份并重建共享索引，由用户审查后决定。任务专属索引放在已分配的 `out/` 目录，不再以 `/tmp` 为默认根。

### 2. 集成提交与防线

`scripts/assert_isolated_index_commit.mjs` 已落地，当前执行方法统一见 [技能的集成与索引流程](../../.codex/skills/team-collaboration-gpt/memory/2026/10/10/team-collaboration-workflow.md#集成与索引)。

- 独立索引只能隔离暂存内容，不能隔离工作树文件，也不能防止旧 HEAD 快照覆盖并发提交；默认由主代理串行集成。
- `git commit -- <路径>` 会绕过隔离索引（事故 `d4e864fc`），禁止使用；禁止 `--amend` 与裸共享索引提交。
- 防线脚本用于事后核验，不是互斥锁。提交前核对 HEAD，提交后核对本笔提交及祖先链；失败需取证与前向恢复。
- 多行提交正文写入任务 `git/` 下的文件，保留真实换行。

---

## 五、规范与实现缺口（历史调查，按当前 owner 与授权推进）

1. **delegated child 准备/发布已收敛到唯一 SQLite owner**：最终 Sol 批次使用 Session `session-control.sqlite` 的 collaboration/creation ledger，child/member 同事务发布，binder 只消费 published readiness；不增加 Team JSON intent 或发布后回滚。team finalizer 真实提交后失败窗口与 351 项 owner 单测已有具名证据，最终核心独审已收口；当前源码已集成e11c8c6b，完整 8.5 验收按原子要求对表，不能整项猜勾。
2. **Resource activation/provenance 真实执行闭包已集成**：`16f09ee7` 接通 execution-owned activation → Runner/Factory/CSM/Provider/Saver 与 Skill/MCP，精确正文 detail、typed/hash、默认 history/API 脱敏及 restore/fork/retention 有具名证据；本地 AES、两个 key 与 thread HMAC 已按用户要求删除。整项门槛按 accepted A01–A03 与当前原任务对表，不重派“尚无生产调用方”。
3. **自由 metadata 决策字段已迁 typed owner**：`ContextContribution` 的 selection_role/replacement_policy/source_binding 与不可变正文合同已接受，middleware 无旧 replaceable_source 双读；producer、封存、恢复与 Provider 按现有收据验收，不恢复旧 metadata 链。
4. **SessionThread 与继承调用闭包已完成最终源码批及独审**：ContextRef、assembly、detail、canonical/checkpoint/fork、GraphBinding/residency 和后台任务携带精确 thread；旧历史 fixture 与 admission 调用方已随批迁齐。真实 cold fork、生成分支、硬重启、通信与 Session URI 联合有具名通过证据；原失败组合保真，产品 Provider403 仍 UNMET，不由 integration 替代。
5. **目录消费者已实现，原导航量化与失败收敛仍在验收**：pending父Folder两UI、Session分页与catalog nodes唯一持续权威已集成e11c8c6b，session.json只留三键identity，零manifest读取及旧同步链清理已有证据。文件202真控件已通过；005修fixture回调风暴，016完成283样本后在结果DOM虚拟化边缘超时，原300文件/40Gateway/20并发量化未完成。Sol006一次修完整测量消费者，不扩大生产hook。10.3旧两测试只查询operation-status，最新confirmed刷新→独立pending本地投影重放仍核真实调用顺序；交同Sol修核心，Luna只独核。
6. **配置来源轴与公共地址字段已完成独立切片**：用户确定 layer 表示逻辑来源，读侧 `sqlite` → `runtime_override`，precedence 不变，active/pending snapshot 从来源清单分开；`f42b6f77` 已实施验收。公共配置字段 `path/schema_path` → nullable `uri/schema_uri` 由 `69d77eea` 完成，R11 独审及 46+10+73+2 项关联验证通过；字段已随统一资源寻址术语迁移正名；其它资源 URI 持久化接线仍须完成。
7. **uuidv7 change** 的前缀数已实核为 33 个、33 个唯一值；自然创建、真实时钟回拨及路径预算已集成验收，49/49 任务完成。关联 SessionThread 与端到端迁移验收仍按 `add-itemized` 第 8 节单独推进，不能由 UUID 验收代替；证据见 `coordinator/artifacts/u07-main-validation.json` 与实时台账。

---

## 六、已登记的环境级基线问题（非本轮引入）

1. **integration 后端进程启动即退出**（`returncode=3`）。
   实测：在 **干净 HEAD 副本**跑 `tests/integration/backend/sessions/test_session_activity.py` 同样 1 error；
   在工作树跑 `test_session_activity.py` + `test_session_generation_strategies.py` + `test_session_history_loading.py` 共 25 errors，**全部是 setup 阶段 `后端进程提前退出`**，与在途改动无关。
   环境细节：`ps` 曾观察到并发 agent 争用同一持久 `gateway.sqlite`、`401 invalid local token`。
   修复 `11dcfa39` 时已用干净副本对照确证因果。
2. `tests/unit/gateway/test_openapi_document_matches_baseline` 的过期哈希（`a0263a90` 已修摘要基线；若仍红需重跑确认）。
3. 历史仓库根曾存在 git-ignored 的 `.boxteam/terminal-manager/`（Sep 29 旧测试状态，workspace_id 为 `gw_terminal_steering_test`）。2026-10-04 已按用户授权删除该目录及空的 `.boxteam/` 父目录，当前不再存在；无法确认具体创建者。

---

## 七、验证基线（本次边界处实测）

```
HEAD                             11dcfa394261e70ffa7aba3a033a285eaebb1bb2
工作树残留                        0
import app.main                   EXIT=0
import app.gateway.main           EXIT=0
openspec validate --strict --all  40 passed / 0 failed
pytest tests/unit/agents/test_llm_logging_middleware.py \
       tests/unit/agents/providers/test_litellm_content.py     29 passed
pytest tests/unit/services/mapping/                            58 passed
pytest tests/contracts/api/                                    76 passed
bun test appStateMaps.test.ts                                   2 pass / 15 expect
bun x tsc --noEmit -p src/clients/web/tsconfig.json             EXIT=0
bun run --cwd src/clients/web build                             EXIT=0
```

受保护三路径全程零触碰：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`。

---

## 八、下一步建议

1. **先检查共享索引与真实工作树**：使用任务新索引，保留现有暂存内容；按技能集中集成。共享索引重建不是继续准备工作的前提。
2. 第五节 1、6 的选择已确定，按对应唯一 owner 继续实现或保持已通过的验收；不重复请求用户裁定。
3. 继续 bug 猎捕时仍未深挖的面：
   - `app/services/infrastructure/{resource_platform, rollout_context, turn_history, team, mcp, node_debug, attachment_*}`；
   - `app/services/orchestration/**` 的执行面（ThreadExecutionQueue、admission ordinal、ExecutionContextFence，OpenSpec 8.3-A）；
   - 前端 `hooks/{session*,sessionEventStream}` 的深层分支、`components/{agentSessions,overlays,shell,eventQueue}`；
   - `tests/integration/**` 的变异鉴别力审计（哪些用例还原缺陷后仍绿）。
4. **并行协作口径**统一见 [团队协作技能](../../.codex/skills/team-collaboration-gpt/memory/2026/10/10/team-collaboration-workflow.md)，不在历史交接中维护第二套指令。保留本轮经验：独占文件范围、独立核验、区分真实缺陷与设计/未接线候选，允许审查者推翻自己的初判。

---

## 九、本轮关键报告与产物路径

- 隔离索引防线脚本：`scripts/assert_isolated_index_commit.mjs`
- 架构与冗余审查：`out/tests/temp/arch_owner_dedup/artifacts/ARCH_REVIEW_round.md`
- 并发/提交独立核验：`out/tests/temp/review_p1_concurrency/artifacts/REVIEW_round2.md`、`out/tests/temp/review_round5/artifacts/REVIEW.md`
- 各面缺陷报告：`out/tests/temp/{infra_bug_hunt,orch_bug_hunt,biz_bug_hunt2,core_keylock,event_stream_hunt,gateway_core_hunt,api_contract_hunt,tests_integration_audit,domain_itemized,core_abstractions}/artifacts/report.md`
- 前端边界：`out/tests/temp/{fe_hooks_deep,fe_api_sse,fe_state_utils,fe_hooks_rest,fe_components_deep,fe_panels_ws,fe_e2e_edges}/artifacts/`
- openspec 台账：`out/tests/temp/{openspec_p2_writer,openspec_audit_round,uuidv7_final_review,openspec_mcp_activation}/artifacts/`
- 上一份 handoff：`docs/handoff/20260929-181500-openspec-vrn-uuidv7-and-persistent-resource-boundary.md`

---

## 十、Was / Wasn't verified

**已验证**：上表全部命令的 EXIT 码与用例数；工作树零残留（独立索引判定）；受保护三路径 blob 未变；`11dcfa39` 与本轮各提交均为 HEAD 祖先，无历史改写。

**未验证**：

- 未跑全量 `tests/unit`（受 AGENTS.md 禁止裸跑约束，且历史上有 OOM 事故）；
- 未起真实浏览器做交互验证（`fe_e2e_edges` 面因 agent 高负载中断，仅计划未执行）；
- 集成测试因环境级 `returncode=3` 无法在本机取得绿灯基线；
- 未 push。
