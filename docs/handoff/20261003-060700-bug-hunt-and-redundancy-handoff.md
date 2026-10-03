# 交接：边界停下（bug 猎捕 + 冗余收敛）

日期：2026-10-03
停止点 HEAD：`11dcfa394261e70ffa7aba3a033a285eaebb1bb2`
工作树状态：**完全干净**（独立索引 `read-tree HEAD` 后 `git status` 零残留）

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

### 已登记、未修的同形状残留

| 位置 | 现状 | 判定 |
|---|---|---|
| `app/core/background_message_bus.py:18 _messages` | 键 `(session_id, agent_id)` 永不回收 | 无界，但它是「供 collect 拉取的 backlog 缓冲」，任何有界化都会改变「稀有会话仍可拉到历史 backlog」的语义，属**产品决策**，需 TTU/会话关闭钩子，未擅改 |
| `app/services/orchestration/thread_residency.py ThreadResidencyTracker._states` | 随 (session,thread) 无界 | 唯一淘汰路径 `sweep()` 全仓无生产调用点；朴素 LRU 会因 generation fence 复用误纳迟到 callback，需 owner 配合 |
| `app/services/infrastructure/trace_event_store.py _event_ids` 的自动 `evt_*` 条目 | 单流有界但自动条目纯冗余 | 涉及契约边界（用例隐含依赖、侧车合并语义），只登记 |

---

## 四、必须交由环境 owner 处理的两项（超出本仓库改动范围）

### 1. 共享 `.git/index` 已损坏（**最高优先级**）

实测状态：

- 索引条目 **3266**，而 HEAD 树条目 **3434**；
- 两份前端 openapi 快照在索引中仍停在旧 blob `eca0c3b7…`，而 HEAD 与工作树均已正确（`586d73ca…`）；
- `git status` / `git diff HEAD` 会把未改动文件谎报成整文件 `D`/`MM`。

**危害**：任何不带 `GIT_INDEX_FILE` 的 `git add`/`git commit` 都会吞并陈旧暂存内容。本轮已真实发生 3 次「陈旧索引快照吞并发提交」事故：

- `bb3f3812` 吞掉 `90130062` → 由 `9b517333` 前向恢复；
- `cbc15fc3` 漏写 `GIT_INDEX_FILE=` 前缀，误用共享索引提交 **701 个并发路径**（含删除 `app/gateway/routes/workspaces.py`、回退两份 openapi 快照）→ 由 `77af33df` 前向恢复，**零内容丢失**（已独立核验）；
- `3ea48090` 用旧 read-tree 快照反向还原 `1287390f` → 由 `911491c2` 前向恢复；
- 另有 `f0711273` 同类自查恢复。

**建议**：显式 `git read-tree HEAD` 重建共享索引（owner 决定）。在修复前，所有 agent 必须走独立临时索引。

### 2. 隔离索引提交防线（已落地，继续强制）

`scripts/assert_isolated_index_commit.mjs`：

```
git rev-parse HEAD
GIT_INDEX_FILE=/tmp/<任务名>.idx git read-tree HEAD
GIT_INDEX_FILE=/tmp/<任务名>.idx git add <精确路径...>   # 绝不用 -A / .
GIT_INDEX_FILE=/tmp/<任务名>.idx git diff --cached --name-only   # 逐条核对
node scripts/assert_isolated_index_commit.mjs record --index /tmp/<任务名>.idx --task <任务名>
GIT_INDEX_FILE=/tmp/<任务名>.idx git commit -F <文件>    # ★ MUST NOT 带 -- <路径>
node scripts/assert_isolated_index_commit.mjs verify --index /tmp/<任务名>.idx --task <任务名>
```

- **`git commit -- <路径>` 会绕过隔离索引**（真实事故 `d4e864fc`）；
- **禁用 `printf '%s'` 写多行提交信息**（曾把换行写成字面 `\n`，两起）；
- 提交前重新取 HEAD 并**立刻** read-tree（缩小并发窗口）。

---

## 五、待 owner 裁定的规范/实现缺口（勿自行改）

1. **`delegate` 的 `before_start` 失败后 child 已 publish，无回收路径**（OpenSpec 8.5 已知缺口）。
   探针实测：child row 与 intent 都留下，`abort_thread_creation_record` 只覆盖 `preparing`，对已 `published` 直接抛错；全库无任何回收已发布 child 的路径。
   二选一：(a) 新增定点回收补偿（跨 5 个 owner，且与 8.5-A「已发布不回退」红线冲突）；(b) 把 `before_start` 下沉到 publish/写 intent 之前。
2. **`Resource activation/provenance` 层尚不存在**（OpenSpec 第 9 节未实施）。
3. **`replaceable_source` 仍暂存在 `ContextItem.metadata`**（`itemized_context_middleware.py` 有 TODO），应迁移为 typed 领域字段。
4. **SessionThread 未成为统一 owner**（`ContextRef.session_id` 无 `thread_id`，OpenSpec 第 8 节要求 `(session_id, thread_id)` 定位）。
5. **前端会话目录 outbox 尚未接线**（`add-itemized-rollout-context` §8.1-H / §10.8 F4，接线门控 §10.1）：`sessionCatalogProjection.ts`(371) / `sessionCatalogOutboxIdbFake.ts`(138) / `sessionCatalogOutboxStore.ts`(221) 生产零调用，但**属在途接线目标，禁止当死码删除**。
6. **`config_sources.py` 的 layer 命名轴不一致**：`inline/user/user_local/workspace` 是来源权威轴，`sqlite` 是存储介质轴；且读侧 `sqlite` vs 写侧 `runtime_override` 异名。需 owner 裁定是否破坏性改名。
7. **uuidv7 change** 的 `tasks.md:107` 前缀数 33 待独立复核；`add-itemized` `tasks.md:174` 迁移测试未收敛。

---

## 六、已登记的环境级基线问题（非本轮引入）

1. **integration 后端进程启动即退出**（`returncode=3`）。
   实测：在 **干净 HEAD 副本**跑 `tests/integration/backend/sessions/test_session_activity.py` 同样 1 error；
   在工作树跑 `test_session_activity.py` + `test_session_generation_strategies.py` + `test_session_history_loading.py` 共 25 errors，**全部是 setup 阶段 `后端进程提前退出`**，与在途改动无关。
   环境细节：`ps` 曾观察到并发 agent 争用同一持久 `gateway.sqlite`、`401 invalid local token`。
   修复 `11dcfa39` 时已用干净副本对照确证因果。
2. `tests/unit/gateway/test_openapi_document_matches_baseline` 的过期哈希（`a0263a90` 已修摘要基线；若仍红需重跑确认）。
3. 仓库存在 git-ignored 的 `.boxteam/`（仅 `terminal-manager`，Sep 29，非本轮产物）。

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

1. **先修共享 `.git/index`**（`git read-tree HEAD`），否则每次裸提交都可能重演吞并事故。
2. 裁定第五节 1、6 两项（其余可继续按既定口径推进）。
3. 继续 bug 猎捕时仍未深挖的面：
   - `app/services/infrastructure/{resource_platform, rollout_context, turn_history, team, mcp, node_debug, attachment_*}`；
   - `app/services/orchestration/**` 的执行面（ThreadExecutionQueue、admission ordinal、ExecutionContextFence，OpenSpec 8.3-A）；
   - 前端 `hooks/{session*,sessionEventStream}` 的深层分支、`components/{agentSessions,overlays,shell,eventQueue}`；
   - `tests/integration/**` 的变异鉴别力审计（哪些用例还原缺陷后仍绿）。
4. **并行协作口径**（本轮已验证有效）：
   - 每个 agent 派单必须写死**独占文件面**，并列出其他在跑 agent 的面以防双写；
   - 每次派单要求「同时发现并减少冗余代码」并回报改造前后行数与 expect/assert 数量变化；
   - 三类结论严格区分：**真实缺陷（改）/ 设计有意（登记）/ 不可达或未接线（登记）**；
   - 鼓励自我推翻（本轮已有 `audit_orchestration_edges` 撤销自身判断、`redundancy_scout_infra` 证明候选不可落地、`be_navigation_bug_hunt` 拒绝删除待接线代码等多例）。

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
