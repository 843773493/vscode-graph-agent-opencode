## ADDED Requirements

### Requirement: 配置变化必须经过统一候选流水线

Gateway 与 Workspace SHALL（必须）对 JSONC 文件变化、配置 API/UI 修改使用同一条候选配置流水线。流水线 MUST（必须）在提交前完成来源解析、优先级合并、schema 校验、业务预检和生效策略分类；只有有效配置发生变化时才能提交新的 active revision。

#### Scenario: 工作区级 JSONC 被修改
- **WHEN** 用户修改当前工作区 `.boxteam/workspace.jsonc`
- **THEN** Workspace 读取完整有效配置并按既有优先级生成候选快照，而不是只解析该覆盖片段或静默忽略变化

#### Scenario: 连续修改多个配置来源
- **WHEN** 用户在一个防抖窗口内连续修改用户级和工作区级配置
- **THEN** 系统合并为一个有序候选提交，并只产生一个对应的有效 revision 变化

#### Scenario: 候选配置无效
- **WHEN** JSONC 语法、schema、版本、工具声明或业务预检失败
- **THEN** 系统保留当前 active snapshot，不得部分应用，并报告具体来源、配置路径和错误原因

### Requirement: 候选配置提交必须原子且可区分待重启状态

系统 SHALL（必须）在配置提交时保持 active snapshot、持久化来源和运行时应用结果的一致性。包含需要重启的配置变化时，系统 MUST（必须）保存已校验的 pending revision，保持旧 active snapshot 继续服务，并明确报告 `restart_required`；不得把同一候选的一部分应用、另一部分丢弃。

#### Scenario: 候选同时包含可热更新和需重启字段
- **WHEN** 一个候选同时修改模型配置和 MCP 配置
- **THEN** 系统不应用模型配置的部分变化，保留旧 active snapshot，并报告完整 changed paths 与 pending restart revision

#### Scenario: 重启后应用待重启配置
- **WHEN** 用户完成对应 Gateway 或 Workspace runtime 的受控重启
- **THEN** pending revision 成为新的 active revision，并清除对应待重启状态

### Requirement: 配置候选必须遵循可恢复状态机

每个配置域 SHALL（必须）为配置提交维护以下状态：`none`、`candidate_validated`、`pending_restart`、`applying`、`active`、`rejected`、`discarded`、`conflict` 和 `recovery_required`。持久化候选 MUST（必须）包含完整规范化候选配置、来源层标识、各来源基线 revision/digest、候选 digest、基于的 active revision 以及配置域的持久化位置；Workspace 候选存放在当前工作区状态 SQLite，Gateway 候选存放在 Gateway 控制面 SQLite。Workspace 的 source layer/desired state、active snapshot 和 pending candidate 必须是可分别读取的记录：导入可先更新 source layer，但 active snapshot 在 promotion 前保持旧值；旧 runtime 只绑定 active snapshot，新 generation 只通过 candidate ref 绑定 pending candidate。

状态只能按以下语义迁移：初始候选经过校验后进入 `candidate_validated`；校验失败进入 `rejected`；CAS/TOCTOU 失败进入 `conflict`；可在当前 runtime 应用的候选从 `candidate_validated` 进入 `applying`，副作用全部成功后进入 `active`；需要重启的候选从 `candidate_validated` 进入 `pending_restart`，用户接受应用后进入 `applying`。`pending_restart` 可在来源基线未变时激活，或在不重叠变化时基于最新 active 重建后替换；用户显式丢弃进入 `discarded`；副作用失败但旧 runtime 恢复成功时回到 `pending_restart`，旧 runtime 无法恢复时进入 `recovery_required`。不得通过重启隐式丢弃 pending。

#### Scenario: 有效候选进入待重启
- **WHEN** 候选通过完整校验但至少一个路径的策略为 `restart_workspace` 或 `restart_gateway`
- **THEN** 系统在对应 SQLite 配置域保存候选、来源层和基线信息，状态为 `pending_restart`，active snapshot 保持不变

#### Scenario: 重启前来源发生变化
- **WHEN** pending 候选等待期间其来源层 revision 或 digest 发生变化
- **THEN** 系统在激活前重新执行 CAS；不重叠变化可以基于最新 active 重建候选，重叠变化进入 `conflict`，不得直接覆盖新来源

#### Scenario: 用户显式丢弃 pending
- **WHEN** 用户显式请求丢弃一个 pending 候选
- **THEN** 系统将其标记为 `discarded`，保留候选和来源摘要，继续使用旧 active，并且在来源 digest 未变化时不重复产生 watcher 事件

#### Scenario: 重启后自动激活 pending
- **WHEN** 服务启动时 pending 的来源基线和候选 digest 仍与持久化记录一致
- **THEN** 系统先完成运行时副作用确认，再原子地将 pending 提升为 active；失败时不得清除 pending

### Requirement: 配置提交 revision 与内容 digest 必须分离

系统 SHALL（必须）分别维护每个配置域单调递增的 `commit_revision`、当前 active/pending 所引用的 revision、完整规范化配置的 `effective_digest`、每个 JSONC 层的 `layer_digest` 和候选 digest。内容 digest MUST NOT（不得）作为提交序号、事件序号或 cursor 使用；没有有效配置变化时不得递增 commit revision。

#### Scenario: 相同内容重复写入
- **WHEN** 文件事件或 API 请求产生与 active 完全相同的规范化内容
- **THEN** 系统保持 effective_digest、commit_revision 和事件 cursor 不变，并报告 unchanged

#### Scenario: 待重启候选提交
- **WHEN** 候选通过校验但进入 pending_restart
- **THEN** 系统可递增持久化 commit_revision 记录候选，但 active revision/effective_digest 仍指向旧 active，直到重启成功

### Requirement: Pending runtime 必须通过启动契约证明配置已加载

任何从 `pending_restart` 启动或重建的 runtime generation SHALL（必须）接收绑定 Workspace、配置域、candidate_id、候选 commit revision、candidate/effective digest、generation token 和过期时间的不透明 `candidate_ref`。Workspace backend 必须通过 Workspace-owned candidate loader 使用该 ref 从 Workspace SQLite 读取 pending 的完整候选；有 ref 时不能读 active 代替 pending，无 ref 时不能读取 pending。Gateway 只能传递 candidate_ref、接收 proof 和调用 Workspace lifecycle/API，不能读取或修改 Workspace SQLite。其健康确认 MUST（必须）返回 generation id、实际加载的配置域、`loaded_source`、实际加载的 active/pending revision、effective/candidate digest、secret binding digest 和 candidate id；Gateway 或 Workspace 只有在这些值与待应用候选完全匹配时才能 promotion active。健康 HTTP 状态本身不能作为配置加载证明。

旧 generation 在新 generation 完成健康确认前 SHALL（必须）保持可服务或保持可恢复句柄；新 generation 不匹配、超时或加载失败时必须优先保留/恢复旧 generation，并保留 pending。

#### Scenario: 新 generation 加载目标候选
- **WHEN** Gateway 启动一个带有 pending candidate_id 的新 Workspace backend
- **THEN** backend 的健康响应返回相同 candidate_id、候选 revision 和 effective digest，Gateway 才允许 active promotion

#### Scenario: 新 generation 只返回健康状态
- **WHEN** 新 backend 返回 HTTP healthy 但没有返回或返回不匹配的配置 revision/digest
- **THEN** Gateway 拒绝 promotion，保留旧 generation 和 pending，并报告 config_proof_missing 或 config_proof_mismatch

#### Scenario: 新 generation 启动失败
- **WHEN** 新 backend 无法启动、加载候选失败或在确认期限内没有提供匹配证明
- **THEN** 系统不清除 pending；旧 generation 继续服务或进入恢复流程

#### Scenario: pending candidate 的读取边界
- **WHEN** 新 backend 使用有效 candidate_ref 启动，或旧 backend 在没有 candidate_ref 的情况下启动
- **THEN** 前者从 Workspace-owned SQLite 精确读取匹配的 pending candidate，后者只能读取 active snapshot；candidate 缺失、绑定不匹配或 secret resolver 失败时直接失败，不回退到另一份配置

#### Scenario: Gateway 不越权读取 Workspace 候选
- **WHEN** Gateway 触发 Workspace backend 重启并等待配置证明
- **THEN** Gateway 只传递不透明 candidate_ref 并接收脱敏 proof/结果，不打开 Workspace `.boxteam/` 数据库，也不在 Gateway SQLite 保存 Workspace 候选 payload

### Requirement: Applying 必须具有单占用、fencing 和崩溃恢复协议

每次配置应用 SHALL（必须）创建唯一 `apply_id` 和 attempt_id，并在对应 SQLite 事务中以 active/pending revision CAS claim `applying`。claim MUST（必须）包含 owner、candidate_id、claimed base revision、target generation、lease/deadline 和 fencing token；只有持有当前 fencing token 的 apply 才能写入 promotion、失败或恢复结果。watcher、API/UI 和重启操作不能同时拥有同一配置域的 applying claim。

进程崩溃、超时或 lease 过期后，启动恢复流程 MUST（必须）读取遗留 applying 记录和 runtime generation 证明：若旧 generation 健康且新 generation 无匹配证明，则清理失效 claim 并回到 pending/旧 active；若状态无法证明安全回滚，则进入 `recovery_required`，不得重复盲目应用。

#### Scenario: 两个应用者竞争
- **WHEN** watcher 和 API/UI 同时尝试应用不同候选
- **THEN** 只有一个 apply_id 获得 CAS claim，另一个收到 conflict/busy 并重新读取 active/pending 状态

#### Scenario: 过期 apply 尝试提交
- **WHEN** 旧 apply_id 在 lease 过期或新的 generation fencing 后尝试 promotion
- **THEN** SQLite 拒绝其写入，旧 apply 不能覆盖新的 active/pending 状态或发布成功事件

#### Scenario: 启动发现遗留 applying
- **WHEN** 服务启动时发现未完成的 applying 记录
- **THEN** 系统依据旧/新 generation 的配置证明决定恢复到 pending 或进入 recovery_required，并记录新的恢复 attempt_id

### Requirement: Gateway 自身 pending 重启必须由持久启动意图驱动

Gateway SHALL（必须）为自身的 `restart_gateway` 维护独立于 Workspace backend 的 pending candidate、active snapshot、runtime generation、restart intent 和 apply claim。普通崩溃恢复与用户确认的 pending 应用 MUST（必须）使用不同的判定路径：没有未过期且完全匹配的 restart intent 时，只能从 active snapshot 启动并保留 pending；用户确认时，intent 与 apply claim 必须绑定 candidate、source baseline、target generation 和 fencing token。

Gateway 新 generation 的启动契约 MUST（必须）只携带不透明 `gateway_candidate_ref`，由 Gateway-owned loader 读取本 Gateway SQLite 中与 ref 完全匹配的 pending。健康 proof MUST（必须）包含 gateway identity、generation、loaded source、candidate/pending revision、effective/candidate digest、secret binding digest、fencing token 摘要，以及目录/Generator scheduler、health controller、registry batch、SSH tunnel/proxy、Workspace process、remote projection 六类 consumer 的脱敏 proof digest；普通 HTTP healthy 不能代替该 proof。

#### Scenario: 普通 Gateway 崩溃恢复
- **WHEN** Gateway 进程崩溃后重启，SQLite 中存在 pending 但没有未过期的用户确认 intent
- **THEN** 新 Gateway 只加载 active snapshot，保留 pending_restart，并报告这是普通恢复而不是 pending 应用

#### Scenario: 用户确认 Gateway pending
- **WHEN** 用户确认应用一个 `restart_gateway` 候选
- **THEN** 系统在同一事务中创建匹配的 restart intent 和 apply claim，新 generation 只能通过 candidate_ref 加载该 pending

#### Scenario: Gateway proof 不匹配
- **WHEN** 新 Gateway 返回的 candidate、revision、digest、generation 或 fencing token 与 intent 不一致
- **THEN** Gateway 不得 promotion active，旧 generation/active 继续保留，结果为 proof mismatch 或 restart_failed

#### Scenario: Gateway pending intent 过期后显式重试
- **WHEN** pending restart intent 已过期，但其 candidate 仍处于 `pending_restart` 或 `recovery_required`
- **THEN** Gateway-owned loader 拒绝直接加载；用户显式 retry 时必须 CAS 生成新的 target generation、fencing token 和有效期，沿用原 candidate/idempotency，不得静默采用旧 intent

#### Scenario: Gateway 新 generation 启动失败
- **WHEN** 新 Gateway 未健康启动或 proof 校验失败
- **THEN** supervisor 优先恢复旧 generation 或按 active snapshot 重启旧 generation；两者都无法恢复时进入 recovery_required，不能返回重启成功

#### Scenario: Gateway 在 loader 早期失败
- **WHEN** pending candidate 在 Gateway-owned loader 的 schema 校验、secret binding 或运行时初始化之前失败
- **THEN** 只有在 Gateway identity、target generation、fencing token 和未过期 intent 完全匹配时，系统才将 intent/candidate/apply journal 持久化到 `recovery_required` 并释放 claim；过期 intent、身份不匹配或 stale generation 不得改写原 pending

#### Scenario: 受控重启时保留旧 generation
- **WHEN** supervisor 为 pending Gateway generation 进行监听交接并要求旧 Gateway graceful shutdown
- **THEN** 旧 generation 必须以 `active/draining` 保留到新 generation promotion 或明确回退；没有匹配 pending intent 的普通停止才将 generation 关闭
