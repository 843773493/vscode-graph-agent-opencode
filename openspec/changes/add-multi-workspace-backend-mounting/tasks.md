## 1. 已挂载工作区注册表与身份层

- [ ] 1.1 引入进程内权威的「已挂载工作区注册表」：每项含稳定 `workspace_id`（严格 UUID 文本，复用 `app/core/workspace_identity.validate_workspace_id`）与其根目录；提供按 `workspace_id` 的精确查找与「未登记即 fail-closed」行为，不提供默认工作区回退。
- [ ] 1.2 把工作区根目录与 `.boxteam/` 数据目录定位从 `app/core/path_utils.py` 的环境变量单例改为「显式 `workspace_id` → 注册表」；`get_workspace_root()`/`get_boxteam_root()`/`get_sessions_dir()` 不再作为业务解析入口（保留或删除由实施时确认调用方全量平移后决定）。
- [ ] 1.3 把 `get_session_path_resolver()`/`get_session_creation_service()` 的 `lru_cache` 键从「会话根目录」改为 workspace 维度；每工作区独立 catalog 连接与 `SQLiteProcessOwnership` 锁。
- [ ] 1.4 确认 workspace_id 只有**一个**命名空间：寻址层统一使用后端身份 UUID；`app/gateway/workspace_ids.py` 的 `gw_` ID 若保留，MUST 只作 Gateway 控制面内部标识，不进入工作区寻址。且 MUST 与 VRN `workspace` scope 必填 `scope_id` 取值同源（同一个稳定 workspace_id）。
- [ ] 1.5 落实 `scope_id` 推导原则（权威表 R2）：`scope_id` MUST 推导自该 scope 的稳定身份、MUST NOT 硬编码；`workspace` scope 取真实 workspace_id，`gateway` scope 取真实 gateway_id，`inline`（原 `builtin`）scope 取真实 distribution_id（来源与编码已由「统一虚拟资源寻址」change 定稿为 manifest 的 `distribution` + `version`，此处只引用）。
- [ ] 1.6 登记两处既存硬编码违反点并定界：`gateway` 与 `inline`（原 `builtin`）的 scope_id 硬编码违反点**均已修复**：`inline` 由 `298ef599`+`f3bd8213` 装配、`gateway` 由 `64ba30c8`/`53befbfc`/`9881a3b2` 装配，原 `skill_runtime.py` 的 `else "local"` 分支已物理删除；`distribution_id` 来源为发行包 runtime manifest（`packages/launcher/runtime-manifest.schema.json`）的 `distribution` + `version`（编码规则与缺失 fail-closed 见「统一虚拟资源寻址」change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」），装配提交 `298ef599`+`f3bd8213`。**2026-10-01 第八轮复核更正**。
- [ ] 1.7 全仓 `rg` 复核「当前激活工作区 / active workspace / WORKSPACE_ROOT / 进程级单根」全部调用方，形成平移清单，确认无遗留悬空调用。

## 2. 服务图按 workspace_id 分区

- [ ] 2.1 把 `app/container.py` 的 `build_app_container` 从「构造期单根装配」改为「持有注册表 + 按 `workspace_id` 惰性构造并缓存服务图」；同 `workspace_id` 只构造一次（进程内幂等）。
- [ ] 2.2 逐项把设计 D3/D4 与 spec「进程级资源必须按 workspace_id 分区」列举的资源改为按工作区分区：配置服务及其工作区根绑定、工作区活动/资源注册表、持久资源账本、会话生命周期 gate 与 operation lease、Job 事件总线与事件通道、后台任务注册表、消息流与 trace 存储。
- [ ] 2.3 校验分区后的隔离：为同一进程挂载两个工作区，断言 A 的会话/事件/任务/SQLite 状态不出现在 B 的请求结果中，且两工作区锁互不阻塞。

## 3. API 契约的破坏性迁移

- [ ] 3.1 按设计 D1 引入路径段载体 `/api/v1/workspaces/{workspace_id}/...`；保留 `X-BoxTeam-Workspace-Id` 作为 Gateway 代理层等价载体，并实现「两载体不一致即显式失败」。
- [ ] 3.2 实现「缺失 workspace 身份即显式拒绝」：不复用激活态、不使用默认工作区补齐。
- [ ] 3.3 更新 Gateway：仍负责选目标，但 MUST 显式把目标传给后端；移除「每个工作区一个后端进程」的默认假设，使一个后端进程可服务多个工作区。跨 gateway 时**网关授权段 / gateway authority**承载稳定 gateway_id，拓扑为**星型解析 / star-topology resolution**（hub-spoke）；解析命中只返回稳定**资源身份 / ResourceIdentity**与内容、不返回 locator，不可解析 fail-closed；上界（visited set / max_transit_gateways=1 / max_gateway_hops=2 / 总 deadline）作为显式策略常量。解析链本体引用「统一虚拟资源寻址」change。
- [ ] 3.4 校验 gateway 身份的接口前提：`gateway` scope 的 `scope_id` MUST 是**真实 gateway_id**；**来源与注入 owner 已裁定**——owner = Gateway 侧按请求注入，取值按「统一虚拟资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」推导（本 change 只引用、不复述取值规则），**通道与装配均已落地**（`64ba30c8`/`53befbfc`/`9881a3b2`，原硬编码字面量分支已物理删除），跨 gateway 寻址的接口前提已满足（**2026-10-01 第八轮复核更正**）；`gateway` 身份与 workspace 身份同属寻址层身份、都必须显式可表达。
- [ ] 3.5 明确破坏边界并更新对外说明：不带工作区前缀或依赖激活态的既有客户端调用失效。

## 4. 持久化与迁移

- [ ] 4.1 盘点只描述单工作区前提的持久化字段，逐项决定「显式迁移」或「显式失效」，不双读、不留别名。
- [ ] 4.2 实现幂等迁移步骤；迁移遇到旧字段 MUST 显式处理，MUST NOT 静默按旧语义继续解释。
- [ ] 4.3 校验**真实路径 / real path** 不变量（**三层分离 / three-layer separation**）：迁移后新增/变更的持久化记录与 API 响应体 MUST NOT 出现 real path，只允许 `资源身份 / ResourceIdentity` + `虚拟资源地址 / VRN`（+ 独立 revision 字段）。
- [ ] 4.4 记录不可回滚点与其理由；确认回滚边界可被明确陈述。**明确本形态无存量 VRN 数据迁移**：权威表实测 VRN 零落盘（157 live + 44 dev/temp SQLite 对 `boxteam://` 零命中、无 `resource_activation*` 表；`display_uri` 仅测试写入、container 无装配、生产 seal 恒为 `None`），故回滚边界在 VRN 侧为代码层回滚。

## 5. 契约快照与门禁

- [ ] 5.1 重新生成 OpenAPI 快照（`bun run gen:openapi`）与前端类型，修复 `tests/contracts/api/**` 中因路径形态变化而失效的断言。
- [ ] 5.2 确认门禁在快照未更新时 fail-closed（不会静默通过）。

## 6. 测试影响面（因无存量迁移而重述为接口契约层）

因 VRN 零落盘、无存量迁移，本形态的实施风险**主要落在接口契约变更**（路由路径形态、显式身份载体、拒绝路径、分区隔离断言），而非数据迁移。

- [ ] 6.1 「只需加 workspace 参数」层：为受影响的服务层/API 单测显式传入 `workspace_id`（优先复用 `tests/unit/core/catalog_workspace_helper.py` 的 `build_catalog_workspace(tmp_path, workspace_id=...)`），不做语义重写。
- [ ] 6.2 「必须重写」层：重写断言单一工作区/直接读 `WORKSPACE_ROOT`/跨工作区复用解析器实例的用例。
- [ ] 6.3 「不受影响」层：确认纯算法/值对象/语法单测无需改动，避免无谓改动。
- [ ] 6.4 新增接口契约层负向断言：`gateway`/`inline` 的 `scope_id` MUST NOT 为硬编码字面量，`workspace` scope 的 `scope_id` 与 HTTP 显式寻址的 workspace_id 同源。

## 7. 收口手续（具名、对称）与命名一致性

- [ ] 7.1 具名收口（与本 change 对称）：本 change 的 workspace 身份以 **`add-unified-virtual-resource-addressing` 的 requirement「多工作区场景下寻址层必须显式承载 scope_id 身份」**（见 `openspec/changes/add-unified-virtual-resource-addressing/specs/virtual-resource-addressing/spec.md`）为唯一权威定义，本 change 承接其实现细节；收口校验以该具名 requirement 为准，不采用「`rg` 复核全仓」这类无目标的表述。
- [ ] 7.2 校验跨 change 命名一致性：逐字使用**冻结契约 v2** 的 `资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`，无同义异名；且本 change 不复述 scope 闭合集与 scope_id 取值语义、不新增拒绝码（**权威表已下发**，scope 闭集终值与 `builtin`→`inline` 正名以该表为准，本 change 只引用）。

## 8. 校验与收尾

- [ ] 8.1 以本 change 自己的门禁执行 `openspec validate --strict --all`，对全部受影响 change 零失败。
- [ ] 8.2 确认本 change 未修改任何生产代码（`app/**`、`src/**`、`tests/**`），且未触碰受保护路径。

## 9. 全仓越限项登记（判据式，含二级子目录）

判据沿用仓库既有门槛：目标目录 MUST 满足「直接源码文件 ≤20」「单文件 ≤800 行」「不与两个以上领域职责混淆/混合」，任一越界即 MUST 附拆分方案或含文件清单、职责/owner 映射、import graph、行数统计与复核结论的架构审查证据。豁免：生成目录（`app/protocol/generated/**`、`src/**/*protocol*_generated/**`、`src/clients/web/src/types/openapi/**`）由上层 `generated/AGENTS.md`/生成命令声明不可手改，不计越限。

- 实测快照（提交 `69cc08fb`）：**本 change 归口的目录直接 `.py` >20**：`app/services/infrastructure=31`（本 change 2.2 明列「按工作区分区」的 config/事件/资源注册表/任务注册表等即分布于此目录，须按 config/trace/node_debug/session 族下沉）、`app/schemas/internal_v2=28`（本 change 3.1/4.x 的 workspace 身份载体与 DTO 内嵌于此目录，须按 session/config/runtime 子域下沉）。
- **本 change 归口的单文件 >800 行**：`app/gateway/registry.py=2222`（本 change proposal/design 已点名为 Gateway 侧 `workspace_id -> service_url` 分派点，须按注册表读写/生命周期/远程复用职责拆分）、`app/container.py=811`（本 change 2.1 要求改 `build_app_container` 为「持有注册表 + 按 workspace_id 惰性服务图」，须顺带按装配族下沉）、`app/gateway/runtime/controller.py=838`（本 change spec `managed-backend-lifecycle` 的托管后端启动/安全重启/外部探测 runtime 控制点，须按托管启动/重启排空/远程重连职责拆分）、`app/schemas/gateway.py=841`（本 change 的 GatewayWorkspaceListDTO/GatewayRuntime*DTO 等身份与 runtime 状态 DTO，须按 workspace/runtime/connection 子域下沉）。
- 跨 change 归属：`app/gateway/main.py=3823`、`app/gateway/control/gateway_state.py=3186`（现为包 `app/gateway/control/gateway_state/`）、`app/gateway/config.py=2332`（现为包 `app/gateway/config/`）、`app/services/infrastructure/config_service.py=2897`、`app/services/infrastructure/workspace_state_store.py=2374`（实测现为 521 行） 亦在 Gateway/Workspace 控制面，但主归属见对应 change 台账（见 `add-unified-virtual-resource-addressing` 与 `migrate-session-context-uri-to-vrn` 的登记条目），本 change 只引用不重复主张。（**2026-10-01 第四轮复核更正**：`app/gateway/main.py=3823` 与 `app/services/infrastructure/config_service.py=2897` 的行数快照**已随拆分失效**——前者现为装配壳、路由在 `app/gateway/routes/**`、lifespan 在 `app/gateway/lifespan.py`，后者现为包 `app/services/infrastructure/config_service/`；其余行数快照亦须在实施期重取，MUST NOT 据旧值判进度。）

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

- [ ] 生成器版本漂移收口（独立切片，未勾选）：把 `buf.gen.yaml` 的 `stephenh-ts-proto` 锁定到与已提交产物一致的版本（`v2.12.1`）或整体升级到 `v2.12.4` 并接受一次性全量重生成；二者择一，作为独立切片提交，MUST NOT 与任何功能改动混提。完成前 MUST NOT 在并发期执行 `buf generate` / `bun run gen:openapi`。证据：`out/tests/temp/impl_job_gateway_id/artifacts/report.md`（第 85-94、201-202、227 行）。
