当前实施范围（2026-10-15）：A05 挂载生命周期/DTO 完整消费闭包由 `aef566ae` 集成并接受，证据取协作收据 `accepted_A05`。§1–§3 中原13项按固定源码、原测试与009只读投影经主核后勾选；主核收据为 `out/tests/temp/2026/10/09/a06-final-review/coordinator/artifacts/a05-task-projection009-verified/receipt.json`。3.5已在README补齐对外迁移边界；1.7已完成原清单逐路径分类及当前新增命中差集核验，见该项具名证据；9.2/9.3原勾选保持。其余原任务按完整要求映射验收，源码接受不等于整change完成，不按历史单根装配或旧未接线清单重复派实施。

## 1. 已挂载工作区注册表与身份层

- [x] 1.1 引入进程内权威的「已挂载工作区注册表」：每项含稳定 `workspace_id`（严格 UUID 文本，复用 `app/core/workspace_identity.validate_workspace_id`）与其根目录；提供按 `workspace_id` 的精确查找与「未登记即 fail-closed」行为，不提供默认工作区回退。
- [x] 1.2 把工作区根目录与 `.boxteam/` 数据目录定位从 `app/core/path_utils.py` 的环境变量单例改为「显式 `workspace_id` → 注册表」；`get_workspace_root()`/`get_boxteam_root()`/`get_sessions_dir()` 不再作为业务解析入口（保留或删除由实施时确认调用方全量平移后决定）。
- [x] 1.3 把 `get_session_path_resolver()`/`get_session_creation_service()` 的 `lru_cache` 键从「会话根目录」改为 workspace 维度；每工作区独立 catalog 连接与 `SQLiteProcessOwnership` 锁。
- [x] 1.4 确认 workspace_id 只有**一个**命名空间：寻址层统一使用后端身份 UUID；`app/gateway/workspace_ids.py` 的 `gw_` ID 若保留，MUST 只作 Gateway 控制面内部标识，不进入工作区寻址。且 MUST 与 URI `workspace` scope 必填 `scope_id` 取值同源（同一个稳定 workspace_id）。
- [x] 1.5 落实 `scope_id` 推导原则（权威表 R2）：`scope_id` MUST 推导自该 scope 的稳定身份、MUST NOT 硬编码；`workspace` scope 取真实 workspace_id，`gateway` scope 取真实 gateway_id，`inline`（原 `builtin`）scope 取真实 distribution_id（来源与编码已由「统一资源寻址」change 定稿为 manifest 的 `distribution` + `version`，此处只引用）。
- [x] 1.6 登记两处既存硬编码违反点并定界：`gateway` 与 `inline`（原 `builtin`）的 scope_id 硬编码违反点**均已修复**：`inline` 由 `298ef599`+`f3bd8213` 装配、`gateway` 由 `64ba30c8`/`53befbfc`/`9881a3b2` 装配，原 `skill_runtime.py` 的 `else "local"` 分支已物理删除；`distribution_id` 来源为发行包 runtime manifest（`packages/launcher/runtime-manifest.schema.json`）的 `distribution` + `version`（编码规则与缺失 fail-closed 见「统一资源寻址」change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」），装配提交 `298ef599`+`f3bd8213`。**2026-10-01 第八轮复核更正**。
- [x] 1.7 全仓 `rg` 复核「当前激活工作区 / active workspace / WORKSPACE_ROOT / 进程级单根」全部调用方，形成平移清单，确认无遗留悬空调用。
  - 2026-10-15 静态闭包：`out/tests/temp/2026/10/13/fork-contract-implementation/luna_context_owner_cleanup/artifacts/r1891-a05-caller-coverage-closure.json` 对原146路径精确分区为后端/配置/工具33、测试71、前端42，missing/extra/duplicates均为空；归类区分合法Gateway激活选路与Backend显式身份，不把关键字命中等同隐式回退。`r1893-a05-current-caller-delta.json` 沿原rg表达式及排除项核当前集合仍为146路径，新增3、移出3；新增均为Gateway控制面或显式身份测试，无Backend隐式身份，移出的旧fork测试已删除。非src旧路径沿用具名静态分类，本轮不声称其当前源码全部重新语义复核；R1898独审核此边界可支撑清单子项。此项只完成调用方清单与语义复核，复用已接受隔离动态证据，不表示4.3真实路径合同、其它任务或整个change完成。

## 2. 服务图按 workspace_id 分区

- [x] 2.1 把 `app/container.py` 的 `build_app_container` 从「构造期单根装配」改为「持有注册表 + 按 `workspace_id` 惰性构造并缓存服务图」；同 `workspace_id` 只构造一次（进程内幂等）。
- [x] 2.2 逐项把设计 D3/D4 与 spec「进程级资源必须按 workspace_id 分区」列举的资源改为按工作区分区：配置服务及其工作区根绑定、工作区活动/资源注册表、持久资源账本、会话生命周期 gate 与 operation lease、Job 事件总线与事件通道、后台任务注册表、消息流与 trace 存储。
- [x] 2.3 校验分区后的隔离：为同一进程挂载两个工作区，断言 A 的会话/事件/任务/SQLite 状态不出现在 B 的请求结果中，且两工作区锁互不阻塞。

## 3. API 契约的破坏性迁移

- [x] 3.1 按设计 D1 引入路径段载体 `/api/v1/workspaces/{workspace_id}/...`；保留 `X-BoxTeam-Workspace-Id` 作为 Gateway 代理层等价载体，并实现「两载体不一致即显式失败」。
- [x] 3.2 实现「缺失 workspace 身份即显式拒绝」：不复用激活态、不使用默认工作区补齐。
- [x] 3.3 更新 Gateway：仍负责选目标，但 MUST 显式把目标传给后端；移除「每个工作区一个后端进程」的默认假设，使一个后端进程可服务多个工作区。跨 gateway 时**网关授权段 / gateway authority**承载稳定 gateway_id，拓扑为**星型解析 / star-topology resolution**（hub-spoke）；解析命中只返回稳定**资源身份 / ResourceIdentity**与内容、不返回 locator，不可解析 fail-closed；上界（visited set / max_transit_gateways=1 / max_gateway_hops=2 / 总 deadline）作为显式策略常量。解析链本体引用「统一资源寻址」change。
- [x] 3.4 校验 gateway 身份的接口前提：`gateway` scope 的 `scope_id` MUST 是**真实 gateway_id**；**来源与注入 owner 已裁定**——owner = Gateway 侧按请求注入，取值按「统一资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」推导（本 change 只引用、不复述取值规则），**通道与装配均已落地**（`64ba30c8`/`53befbfc`/`9881a3b2`，原硬编码字面量分支已物理删除），跨 gateway 寻址的接口前提已满足（**2026-10-01 第八轮复核更正**）；`gateway` 身份与 workspace 身份同属寻址层身份、都必须显式可表达。
- [x] 3.5 明确破坏边界并更新对外说明：`README.md` 的“工作区 API 的显式身份”说明直接Backend的路径/header载体、缺失/冲突/未挂载拒绝，以及Gateway既有同源路径继续由代理注入UUID的边界；不将所有无前缀Gateway请求误判为失效。后端不依赖激活态补齐身份。

## 4. 持久化与迁移

- [ ] 4.1 盘点只描述单工作区前提的旧持久化字段，按用户授权显式失效并删除旧读取分支，保留新写字段与单一读路径，不双读、不留别名。
- [ ] 4.2 核验旧开发运行数据清理与新 writer 切换：停止对应写入后清理不兼容开发数据，保普通源码及独有未集成改动；不新增旧数据迁移/扫描/回填，fresh writer 与精确恢复错误必须修复并验证。
- [ ] 4.3 校验**真实路径 / real path** 不变量（**三层分离 / three-layer separation**）：迁移后新增/变更的持久化记录与 API 响应体 MUST NOT 出现 real path，只允许 `资源身份 / ResourceIdentity` + `资源 URI`（+ 独立 revision 字段）。
- [ ] 4.4 记录代码、数据与生成快照的回滚边界及不可回滚点：旧开发数据按授权失效/清理，不承诺恢复；新形态数据不能交旧 reader。原零 URI、无装配、seal 恒为空是历史取证，A01 `16f09ee7` 已接通真实 activation freeze/seal/restore，不重复派发已完成接线，不虚构旧 URI 迁移。

## 5. 契约快照与门禁

- [ ] 5.1 重新生成 OpenAPI 快照（`bun run gen:openapi`）与前端类型，修复 `tests/contracts/api/**` 中因路径形态变化而失效的断言。
- [ ] 5.2 确认门禁在快照未更新时 fail-closed（不会静默通过）。

## 6. 测试影响面（因无存量迁移而重述为接口契约层）

本形态不迁移旧开发数据；验收重点为接口契约（路由路径形态、显式身份载体、拒绝路径、分区隔离）、新写入和精确恢复，不能沿用历史零 URI 取证判断当前生产状态。

- [ ] 6.1 「只需加 workspace 参数」层：为受影响的服务层/API 单测显式传入 `workspace_id`（优先复用 `tests/unit/core/catalog_workspace_helper.py` 的 `build_catalog_workspace(tmp_path, workspace_id=...)`），不做语义重写。
- [ ] 6.2 「必须重写」层：重写断言单一工作区/直接读 `WORKSPACE_ROOT`/跨工作区复用解析器实例的用例。
- [ ] 6.3 「不受影响」层：确认纯算法/值对象/语法单测无需改动，避免无谓改动。
- [ ] 6.4 新增接口契约层负向断言：`gateway`/`inline` 的 `scope_id` MUST NOT 为硬编码字面量，`workspace` scope 的 `scope_id` 与 HTTP 显式寻址的 workspace_id 同源。
  - 2026-10-15 workspace 子项：`test_mounted_workspace_lifespan.py::test_real_double_mount_requests_compilation_and_reverse_cleanup` 经正式显式路径读取两个真实工作区的消息，核对 DTO 身份回显及正文隔离；把 B 的完整引用提交给 A 的路径明确返回 `400/scope_mismatch`，不返回 B 的数据或资源身份。R2231 单节点通过，原件在 `out/tests/temp/2026/10/13/fork-contract-implementation/luna_remaining_acceptance_focus/artifacts/runs/r2231-mounted-messages-runtime-01/`，主核收据为 `coordinator/artifacts/mounted-and-skill-original-verification-1797.json`。本证据不单独完成 gateway/inline 子项，不勾整个 6.4。

## 7. 收口手续（具名、对称）与命名一致性

- [ ] 7.1 具名收口（与本 change 对称）：本 change 的 workspace 身份以 **`add-unified-virtual-resource-addressing` 的 requirement「多工作区场景下寻址层必须显式承载 scope_id 身份」**（见 `openspec/changes/add-unified-virtual-resource-addressing/specs/virtual-resource-addressing/spec.md`）为唯一权威定义，本 change 承接其实现细节；收口校验以该具名 requirement 为准，不采用「`rg` 复核全仓」这类无目标的表述。
- [ ] 7.2 校验跨 change 命名一致性：逐字使用**冻结契约 v2** 的 `资源身份 / ResourceIdentity`、`资源 URI`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`，无同义异名；且本 change 不复述 scope 闭合集与 scope_id 取值语义、不新增拒绝码（**权威表已下发**，scope 闭集终值与 `builtin`→`inline` 正名以该表为准，本 change 只引用）。

## 8. 校验与收尾

- [ ] 8.1 以本 change 自己的门禁执行 `openspec validate --strict --all`，对全部受影响 change 零失败。
- [ ] 8.2 按固定实现与原 task ID 核验已授权的 `app/**`、`src/**`、`tests/**` 改动及真实同进程隔离、显式身份拒绝和 runtime 生命周期证据，确认没有遗漏的调用方或旧双轨；受保护路径必须零改动。原“只写规划、不得改生产代码”的阶段限制已由用户授权实施取代。

## 9. 全仓越限项登记（判据式，含二级子目录）

判据沿用仓库既有门槛：目标目录 MUST 满足「直接源码文件 ≤20」「单文件 ≤800 行」「不与两个以上领域职责混淆/混合」，任一越界即 MUST 附拆分方案或含文件清单、职责/owner 映射、import graph、行数统计与复核结论的架构审查证据。豁免：生成目录（`app/protocol/generated/**`、`src/**/*protocol*_generated/**`、`src/clients/web/src/types/openapi/**`）由上层 `generated/AGENTS.md`/生成命令声明不可手改，不计越限。

- 当前固定快照（提交 `1c20cc572039e8905483c0c8646a46d69f38055f`，2026-10-05 主线程与只读审查者分别核数）：**本 change 归口的目录直接 `.py` >20**：`app/services/infrastructure=34`（2.2 的配置、事件、资源与任务注册表等所在目录）、`app/schemas/internal_v2=29`（3.1/4.x 的 workspace 身份载体与 DTO 所在目录）。必须按上述判据提供职责/owner、import graph及复核结论；发现可独立的职责族时下沉，不仅为数量创建薄包装。
- **本 change 归口的单文件 >800 行**：`app/container.py=992`（2.1 工作区服务图装配）、`app/gateway/runtime/controller.py=921`（托管后端启动、重启排空与远程重连）、`app/schemas/gateway.py=855`（Workspace、runtime与connection DTO）。`app/gateway/registry.py` 已物理下线为 `app/gateway/registry/` 包，原2222行快照失效，不重复实施已有拆分。
- 跨 change 归属：`app/gateway/main.py` 当前59行；`app/gateway/control/gateway_state/`、`app/gateway/config/`、`app/services/infrastructure/config_service/` 已是职责包，旧单文件行数不再作为进度或缺陷依据。其主归属仍见「统一资源寻址」与会话上下文资源 URI change；本 change 只引用。**本节尚未收口**：数字核验不能代替要求的文件清单、owner映射、import graph与架构复核；实现期只针对真实职责混合补齐证据或拆分，不据旧统计重建已有能力。

- [x] 9.1 固定1c20的63个直接Python文件已完成逐文件owner、LOC及反向import graph审查，report/inventory位于 `out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/mounting-architecture-review/attempt-1/`。container与Gateway runtime controller仍是连贯的装配/生命周期owner，保留；以下两项有职责证据，仍待实现，不因统计完成勾整节。
- [x] 9.2 按既有Gateway DTO领域owner拆分 `app/schemas/gateway.py` 为职责包；核全仓直接/间接import及测试，将消费者平移至具体模块后删除旧文件，不留re-export兼容层、不改公开字段或行为。审查固定树有32个生产import模块，实施前重新清点完整当前调用方。
- [x] 9.3 将 `message_stream_store.py` 中只计算传入state/event的纯reducer/helper提为领域明确模块；Store保留同一turn锁和 `_commit_locked` 的 reducer→durable append/fsync→cache→fanout 顺序与恢复/游标owner，验证原事件投影和持久化行为不变，不拆第二事务owner。

### 2026-09-30 补登记：gateway scope_id 的请求级注入通道（跨 change 落差审计 F3）

本条补「登记了来源裁定、未登记载体」的缝隙（审计见 `out/tests/temp/cross_change_gap_audit/artifacts/`）：3.4 与本 change 的 gateway 身份裁定只说「剩装配」，但没有任何任务承接「Gateway 侧按请求注入 `X-BoxTeam-Gateway-Id`，并把它接进工作区后端的 `ResolutionContext` / `require_scope_binding`」。载体未闭合处已实测：`app/agents/skill_runtime.py:522` 的 `_layer_scope_identity` 对 `gateway` scope 仍返回字面量 `local` 与 `None`（跳过绑定校验），该文件 `:507-513` 的 TODO 自述需连锁改造全部 router。

- [x] 3.4-A 实现 gateway scope_id 的请求级注入通道：Gateway 在自有 API 与代理 API 上按请求注入并经 `X-BoxTeam-Gateway-Id` 透传真实 `gateway_id`；工作区后端在请求作用域内取得该值并构造 `ResolutionContext`，使 `app/agents/skill_runtime.py` 的 `gateway` scope 不再返回字面量 `local`、不再跳过 `require_scope_binding` 校验。门槛：新增测试覆盖「带 `X-BoxTeam-Gateway-Id` 的请求在 `gateway` scope 解析出真实 gateway_id 并通过绑定校验」与「缺失该头的请求 fail-closed（不得回退字面量 `local`）」；移除注入后对应用例变红（贴原始输出）；`skill_runtime.py` 中该分支的字面量回退被物理删除（全仓 `rg` 复核无残留）。
  - 落地提交 `64ba30c8`（父 `ccec61a0`）。请求级注入通道复用 TraceMiddleware 的 ContextVar（与 `X-Request-ID` 同生命周期）：Gateway 在自有 API 与代理 API 注入 `X-BoxTeam-Gateway-Id`（取值见 `app/gateway/proxy_upstream.py::load_proxy_gateway_id`），工作区后端从请求上下文读入并接通 `ResolutionContext` / `require_scope_binding`；job 为独立执行根，`gateway_id` 随 `JobState`/`JobRuntimeState` 显式携带并在 `JobExecutionService.run` 绑定。
  - 聚焦套件（`test_skill_runtime` + `test_skill_catalog_layers` + `test_trace_middleware` + `test_proxy_header_boundaries`）：69 passed（EXIT 0）。
  - 广域：`tests/unit/agents` + `tests/unit/core` + `tests/unit/services/business/job` = 1501 passed / 7 skipped；`tests/unit/gateway` = 436 passed；`tests/unit/api` + 基础设施 2 文件 = 150 passed。
  - 变异（贴原始输出）：在 `git archive HEAD` 解包的独立树把 `_layer_scope_identity` 的 gateway 分支还原为 `return "local", None`，恰 4 条 gate 用例变红：`test_build_workspace_skill_catalog_resolves_gateway_layer`、`test_gateway_scope_uri_passes_binding_with_injected_identity`、`test_build_workspace_skill_catalog_fails_closed_without_gateway_identity`、`test_gateway_identity_flows_from_request_header_into_catalog`（4 failed, 65 passed；该树基线 69 passed）。
  - 全仓 `rg` 复核字面量回退已物理删除：`rg -n local app/agents/skill_runtime.py` 仅剩 2 处说明性文本（`:520` docstring、`:616` 注释），无 `return "local"` / `return "local", None` 代码残留；`_layer_scope_identity` 另补未知 layer 的结构化拒绝。
  - 证据：`out/tests/temp/impl_gateway_scope/artifacts/report.md`、`mutation_restore_local.txt`、`focused_run.txt`。
  - **owner 裁定（2026-09-30，书面确认）：接受触发条件收窄。** 规范字面「缺失该头的请求 fail-closed」与实现口径「**fail-closed 触发条件为『gateway 层有条目』**」不再隐式分歧：gateway 层无条目时不物化任何 gateway-scope URI、不产生任何虚假身份（不产生 `local`、也不产生任何其它身份），此时不强制 fail-closed；理由是该收窄对应 3.4-A 的实质目的「不得用字面量 `local` 或任何虚假身份静默解析 gateway-scope 资源」，而 gateway 层无条目时不存在待解析的 gateway-scope 资源，强制 fail-closed 只会打断 inline/workspace-only 路径且无安全收益。
- [x] 3.4-B 重启恢复的 pending job 的 gateway 身份载体：`PendingRequestDTO` 新增必填 `gateway_id: str | None`（proto `optional string gateway_id = 18`），落盘内容由 `pending_request_controller._dto()` 从 `job.gateway_id` 填充；磁盘恢复时 `JobState(gateway_id=record.gateway_id)` 逐字读回，删除了 `app/services/business/job/service.py` 旧 TODO。缺该字段的老数据在 `pending_request_store.load` 的 `model_validate` 处 `ValidationError`，被包成 `RuntimeError("待处理队列恢复失败: ...")`，为刻意的**诚实失败**；**未**用进程级单例、字面量 `local` 或默认值补齐。落地提交 `53befbfc`（父 `76ed0089`）。
  - 门槛：新增「落盘再读回 gateway_id 逐字一致」「老数据缺字段 fail-closed」「恢复的队首/队尾 Job 沿用持久化 gateway_id」三条聚焦用例；变异（恢复点改 `None` / DTO 字段给默认值）对应用例各 1 条变红（原始输出见报告）。
  - 聚焦 68 passed；广域 `tests/unit/{services/business/job,services/infrastructure,api,agents,core}` = 2530 passed / 7 skipped；前端 25 pass 且 `bun run build` 通过；`openspec validate --strict --all` = 40 passed / 0 failed；`import app.main` / `app.gateway.main` 退出 0。
  - 生成物：`proto/boxteam/workspace/v2/public.proto` 加字段后重生成 6 个公开绑定文件 + `bun run gen:openapi` 两个快照。证据：`out/tests/temp/impl_job_gateway_id/artifacts/report.md`。
  - **owner 裁定（2026-09-30，书面）：① 生成器版本漂移不锁定为临时手段。** `buf.gen.yaml` 未锁定 `protoc-gen-ts_proto`（现解析 v2.12.4、已提交产物 v2.12.1）属既有工具链缺口，**不在本笔修复**；本次临时锁定 v2.12.1 重生成并逐字还原 `buf.gen.yaml`（sha1 与 HEAD 一致）为正确处置。锁版本 / 整体升级作为**独立切片**另行登记，不得夹带进功能提交。**② `gateway_id` 保留在对外响应体。** 理由：`PendingRequestDTO` 是恢复通道的既有唯一载体，为剔除一个字段另建内部持久化模型会引入第二套模型（AGENTS.md 严禁双轨/过度抽象）；该字段是**稳定标识、非 locator/credential**，不违反「real path 永不进响应体」；前端零消费、不影响 UI。故**接受**其进入 `openapi.json`。
- [x] 3.4-C gateway 身份头在三条 Gateway 代理通道上的补齐：`X-BoxTeam-Gateway-Id` 原先只在工作区 API HTTP 代理（`app/gateway/server/workspace_proxy.py::_proxy_headers`）与辅助服务 HTTP 代理（`app/gateway/auxiliary_proxy.py::_proxy_request_headers`）注入，本次按同一注入 owner（Gateway 侧，取值 `app/gateway/proxy_upstream.py::load_proxy_gateway_id`）补齐以下三条通道：(1) WebSocket 中继 `app/gateway/auxiliary_proxy.py::_proxy_auxiliary_websocket`（`upstream_headers`）；(2) 生命周期/配置 control-plane `app/gateway/runtime/controller.py::_backend_headers`；(3) 联邦冷 catalog 端口 `app/gateway/federation/workspace_port.py::WorkspaceCatalogPort._export`。三条都不转发客户端传入的同名头——WS 握手只发送代码内显式写入的头部，两条 httpx 请求头部亦为代码内构造，故无需额外剥离。
  - 落地提交 `9881a3b2`（父 `3f40f15a`）。
  - 聚焦测试 `tests/unit/gateway/test_gateway_identity_proxy_channels.py`（三条通道各一条用例，断言注入值为本机 `identity.json` 的真实 gateway_id）：`test_backend_headers_inject_authoritative_gateway_id`、`test_workspace_catalog_port_injects_authoritative_gateway_id`、`test_auxiliary_websocket_injects_authoritative_gateway_id`。把三处注入还原后 3 条全红；`tests/unit/gateway/` 全量 440 passed。
- [x] 3.4-D 前端会话列表收敛必须按活动工作区守卫（**新发现，已修复**）：`src/clients/web/src/hooks/session/useSessionLifecycleActions.ts` 的 `applySessionListConvergence` 原先**无条件** `next.sessions = remainingSessions` 覆盖全局 `state.sessions`，而 `remainingSessions` 来自 `apiListSessions(apiPort, workspaceIdForRequest)`，其 `workspaceIdForRequest` 可由上游传入**非活动工作区 B**（`AgentSessionsContextMenus.tsx` 传 `target.workspaceId`）。后果：在活动工作区 A 时从资源树删除属于 B 的会话，会污染全局会话列表（时间分组视图错显 B 的会话、`selectSession` 报「不存在会话」）。修复：按同文件已有先例（`forkSessionContext`/`setSessionParent`）加 `if (state.activeGatewayWorkspaceId === workspaceId)` 守卫，仅在该会话属于活动工作区时同步全局列表；未发明新机制、未新增抽象。落地提交 `b28ad9a4`。
  - 门槛：新增跨工作区聚焦用例，精确断言（`toEqual`，非 `toBeTruthy`/`toContain`）「非活动工作区镜像收敛、全局 `sessions` 不变、活动工作区不变」；去掉守卫后该用例变红（贴原始输出）。focused `bun test` 17 pass、`tsc --noEmit` EXIT=0、`bun run --cwd src/clients/web build` EXIT=0。证据：`out/tests/temp/fix_session_convergence/artifacts/report.md`。
  - 审查提示：同源嫌疑路径 `src/clients/web/src/state/session/sessionRefresh.ts:99` 未在本切片核实，**列入后续复核**（不得声称已全覆盖）。

### 2026-09-30 补登记：Gateway 上下文只读端点免凭据为既有契约，非缺陷（F4）

本条登记 **owner 已裁定的既有契约**，MUST NOT 被读作「待修缺陷」，亦 MUST NOT 登记为「已修复」。来源：`out/tests/temp/gateway_edge_audit/artifacts/report.md` 第 79 行起（F4）。

- **实测位置（2026-09-30 主 agent 复跑 `rg`/读文件核对）**：`app/gateway/server/workspace_proxy.py:549-561` 的 `proxy_context_read`（路由 `POST /api/v1/context/read`）与 `:564-576` 的 `proxy_context_search`（路由 `POST /api/v1/context/search`）**均已显式书写** `auth=None`、`user_access=None`、`include_credentials=False`（逐行见 `:558-560` 与 `:573-575`），两函数 docstring 逐字写明「不要求 Gateway 凭据」。对照 `:579-594` 的兜底路由 `/api/v1/{path:path}`，其依赖为 `auth: GatewayAuthContext = Depends(verify_gateway_access)`（`:586`）与 `user_access = Depends(verify_user_access_for_proxy)`（`:587`）、`include_credentials=True`（`:594`）。
- **免鉴权行为已被集成测试固化为既有契约**：`tests/integration/gateway/test_gateway_workspace_routing.py:417`（`test_session_context_tools_query_another_workspace_through_gateway`）在 `:498-504` 断言未鉴权 `POST /api/v1/context/read` 返回 200。
- **owner 裁定（2026-09-30，书面）：不修。** 依据：本项目为单用户本地程序（AGENTS.md「没有云服务功能」「没有多租户」），且用户既定立场为「当前项目暂且不设权限」；代码把 `auth=None, user_access=None` 显式写出，属**刻意的显式设计**而非遗漏。故本项登记为**既有契约的显式登记**，不是待修缺陷。
- **重新裁定触发条件（MUST 保留）**：若将来引入权限模型，本项是必须**重新裁定的入口清单之一**；在重新裁定前，任何单方面给这两个端点加 `verify_gateway_access` 的改动都必须同步调整上述集成测试，不得以本登记为依据声称该行为「已修复」或「已收紧」。
- MUST NOT 把本项登记为「已修复」；本 change 在本条不实施任何生产改动。

### 2026-09-30 补登记：`buf.gen.yaml` 生成器版本漂移 = 独立待办切片（3.4-B 遗留）

本条把 3.4-B 的 owner 裁定（见上文 `### 2026-09-30 补登记：gateway scope_id...` 内 3.4-B 的「① 生成器版本漂移不锁定为临时手段」）**落为可机械复核的独立待办切片**。

- **实测事实（2026-09-30 主 agent 复跑）**：`buf.gen.yaml` 的 ts-proto 插件声明为 `- remote: buf.build/community/stephenh-ts-proto`（`buf.gen.yaml:16`），**未锁定版本**（无 `:vX.Y.Z` 段）。已提交生成物 `src/clients/web/src/types/protocol_generated/**` 的版本头逐字为 `protoc-gen-ts_proto  v2.12.1`（实测 23 个 ts-proto 文件版本头一致）。解析侧当前为该插件的最新发布 `v2.12.4`（`npm view ts-proto version` = 2.12.4；仓库无 `buf.lock`）。
- **漂移后果**：任何一次 `buf generate`（经 `bun run gen:protocol` / `scripts/generate_protocol.mjs` 调 `buf generate`）或 `bun run gen:openapi`，都会把全部 ts-proto 产物的版本头从 `2.12.1` 改成 `2.12.4`，形成与本次语义无关的**非预期生成物漂移**。
- **owner 裁定（2026-09-30，书面）**：锁版本或整体升级作为**独立切片**，MUST NOT 夹带进任何功能提交；在独立切片完成前，任何 `buf generate` / `bun run gen:openapi` 都会引入上述非预期生成物漂移。

- [x] 生成器版本漂移收口（独立切片）：`7596a841` 将 ts-proto 固定为当前产物 `v2.12.1`；真实生成又暴露 es header 漂移，故同时固定两个 es 为 `v2.15.0`、python 为 `v36.2`。`bun run gen:protocol` 退出 0，四个生成根相对 HEAD 零差异；Python/Node 跨文件 oneof 绑定测试各 1 项通过。未升级业务协议或夹带功能修改。证据：`out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/g03-validation.json`。后续功能切片在独立 worktree 使用此配置生成，主树串行集成。

## 10. 共享 Backend 生命周期收口（唯一合同 owner）

本节承接原3.3、8.2及D9的共享生命周期缺口，不重复实施已经受验的后端双mount寻址/隔离。唯一合同见本 change 的 `managed-backend-lifecycle` delta；同一作者一次迁齐源码、DTO/调用方和核心公共fixture，少量真实核心修通后集中补边界，最终停写只冻结一次完整候选。

- [x] 10.1 将共享 Backend 进程handle/generation/显式完整启动清单归一个Gateway managed owner；成员只引用，辅助服务仍按target拥有。控制面只维护一份共享成员权威，不按相同URL或运行target推导组。普通新增保持独立singleton启动，attached/external及默认target限制不变。
- [x] 10.2 沿现有lifecycle入口贯通完整组 `expected_backend_generation`/`expected_workspace_ids` 前置条件及owner级串行锁；共享组普通单target操作blocked且不改成员意图。实现generation内mount集合不可变，显式成员进退经全组drain/重建/验证后才公布，失败恢复完整旧组。 明确成员变更独立的desired_workspace_ids完整目标集合，joining项须已登记managed且不归其它活动owner；失败以旧已公布集合为唯一权威，无法恢复时全组offline，明确完整组start是恢复出口。包含默认成员的整组stop、移除默认成员在drain前拒绝。
- [x] 10.3 汇总全组Job/tool/background及proxy request/stream blockers；safe沿原30秒期限，超时撤销drain并保旧组。force先持久化全部受影响执行interrupted再唯一close；贯通 `backend_generation`、`affected_workspace_ids` 和blocker `workspace_id` 至DTO/OpenAPI/协议、Web请求/阻塞显示/确认及fixture，不混Gateway target ID与Backend UUID。
- [x] 10.4 统一registry remove/upsert/runtime替换、stop/close、Gateway shutdown/preserve/adopt/startup recovery按owner一次处理；共享故障使全成员offline/路由失效，按完整启动合同只恢复一次，UUID/root/membership不符明确拒绝。最后成员退出只关闭Backend一次。 覆盖Gateway候选proof/CAS成功前保旧owner/handle/完整启动清单、proof失败旧组继续服务和新旧均失效时完整组恢复，禁止部分/重复adopt。
- [x] 10.5 配置pending restart沿原candidate/journal/proof状态机执行整组切换，只promotion发起成员的匹配candidate，sibling只核自身身份/健康；失败恢复完整旧generation/mount集合，补偿失败按原recovery_required反馈。 proof missing/mismatch及recovery_required的retry/resolve/discard须绑定owner generation与完整旧/目标mount集合，全部sibling身份/健康未确认不公布候选或恢复generation在线；普通非配置offline恢复不新造配置状态机。
- [x] 10.6 先跑真实一个Gateway/一个managed Backend/两个mount核心：身份/数据隔离、普通单targetblocked、明确完整组重启及handle只关闭一次；随后集中覆盖陈旧generation/成员集、同组并发、sibling blockers/force、成员进退、故障及一次恢复/adopt、配置补偿、external同URL和默认target限制，带进程外保护并保真实退出码。 同批边界补目标集合输入、切换与旧组补偿均失败后的完整组start恢复、默认共享成员stop/remove拒绝、Gateway generation proof成败及配置域retry/resolve/discard的完整组证明。
- [x] 10.7 按新完整候选独立核全部生命周期调用方及旧handle/逐target恢复链已物理下线，复用依赖未变证据、增审受影响差异；完成静态/生成类型/Web构建及相关真实客户端验收，再更新本节与原关联任务，不以规划或核心绿灯勾整个change。

10.2/10.4/10.6 的最后成员闭包验收同时覆盖：发起成员退出合法desired集合；显式空desired与缺省presence在HTTP及生成协议往返中严格区分；空目标不启动Backend；普通DELETE的真实Backend blocker返回409且旧owner/target/意图不变；显式force先持久化interrupted、唯一close与辅助清理，再返回完整WorkspaceList。同步迁Web确认/观察租约、直接托管与联邦删除caller，并核registry.remove/runtime替换没有活跃managed close旁路；相同候选已通过的其它主题只复用或增审差异。

同一闭包还须验停机共享组的非末成员DELETE：无Backend启动、剩余全组offline/停止意图/无runtime lease、新generation使旧请求陈旧、后续完整start验证剩余启动合同；清理或发布失败保旧owner/generation/成员/登记。停机safe/force沿原路由引用owner执行。非managed远程DELETE须在SSH转发清理成功后移除登记，502时保target和未清理转发，修复后可重试清理。上述增量已完成固定增审，仅本闭包勾选完成；原 change 其余任务保持各自验收范围。

2026-10-08 A05验收：169源码＋7主文档完整候选继承已验基线，严格补丁重建通过；L2停机成员退出35核心＋21相关边界＋1真实Gateway通过，L3远程转发1唯一失败/重试用例通过。固定独审确认L1/L2/L3与C1/C2已关闭，复用未变动态与批末Webbuild/strict；原643Gateway结果晚于多数改动、早于最后HTTPError catch，catch由最终7精准例补验，不称643最终重跑。报告和完整候选见 `out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/mounting-lifecycle-dto-source-bulk-dispatch/ROOT-full-final-candidate/revision3/`，L2独审见同run的 `wire_contract_reviewer/artifacts/a05-stopped-member-exit-revision3-review/`。9.2/9.3及第10节完成不代表3.3/8.2等整个change收口，真实Provider403和原goal剩余验收保留。
