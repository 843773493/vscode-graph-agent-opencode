## Context

当前 Workspace 已有 JSONC 文件监听、候选快照和 `mcp`/`logger` 重启拒绝逻辑，但 Gateway 只在 lifespan 启动时读取配置。正常容器还会把可变配置迁移到 SQLite，导致文件监听与实际有效来源脱节。当前 Job 事件总线按 job/session 分区，不能直接作为全局配置事件流。`reference_repo/vscode` 的可复用模式是：配置源变化后短暂防抖，重新生成完整配置模型，再发出配置变化事件；它不会把所有变化都假设成同一种运行时更新。

本变更必须保留现有配置域隔离：Gateway 不读取工作区业务配置，Workspace 的最终配置仍按既有优先级合并；SQLite 仍是共享可变配置的持久化边界。

## Goals / Non-Goals

**Goals:**

- 让 JSONC、SQLite 和 API/UI 修改进入同一套可验证、可观察、可恢复的配置变更流水线。
- 为 Gateway 增加类似 VS Code 的防抖重载和配置变化事件。
- 为 Workspace/Gateway 建立集中维护的生效策略，区分当前请求、后续 Job、后续会话、运行时重建和受控重启。
- 让需重启配置与现有排空、pending revision、失败回滚和远程委托生命周期衔接。
- 让诊断结果能回答“当前使用哪一份配置、这次改动是否已提交、何时生效、是否需要重启”。
- 让重启、热应用和恢复失败都有明确持久化状态，不能依赖进程内布尔值在重启后猜测。

**Non-Goals:**

- 不把工作区 `.boxteam/` 配置提升为 Gateway 配置，也不允许 Gateway 直接读写工作区业务数据。
- 不为每个配置字段提供任意用户自定义的热重载策略；策略由产品契约集中定义。
- 不在本变更中重写 VS Code 的全部配置模型、设置 UI 或 JSONC 注释保留编辑器。
- 不自动强制中断活动 Job；需重启配置默认等待用户确认和现有排空规则。

## Decisions

### 1. 采用“完整候选快照 + 事件”模型

每次源变化都重新生成完整有效配置，而不是让消费者读取某一个变更文件并自行合并。每次尝试先分配 `candidate_id` 和 `attempt_id`，候选依次经过来源解析、优先级合并、schema/业务校验、diff 和应用策略分类；提交后以 immutable snapshot 作为运行时输入，并发布带独立 cursor 的变化事件。候选状态持久化在对应配置域 SQLite：可热应用路径走 `none → candidate_validated → applying → active`，需重启路径走 `none → candidate_validated → pending_restart → applying → active`，失败分支为 `rejected`、`discarded`、`conflict` 或 `recovery_required`。

这对应 VS Code 的 `loadConfiguration()` 与 `onDidChangeConfiguration` 关系，同时避免各服务维护第二套合并逻辑。物理关系固定为 `source layers / desired state → active snapshot（旧 runtime）或 pending candidate（新 generation）→ candidate health proof → promotion active`。候选失败时继续使用最后一个有效 snapshot，需重启候选则保存 pending revision，不部分应用。pending 记录保存完整规范化候选、来源层、来源基线、候选 digest、基于的 active revision 和持久化位置；重启只在来源基线未变时自动激活，否则先 CAS/重建或进入 conflict。

备选方案是仅在各消费者读取配置时重新读取文件；该方案无法提供一致的 revision、冲突检测和失败状态，因此不采用。

### 2. SQLite 仍是运行时权威，文件变化作为带基线的输入

配置记录增加来源标识、最近导入 digest、active/pending revision 和基线信息。监听器读取 JSONC 的稳定字节快照，记录各层 `layer_digest`/`layer_revision`，在解析前后再次确认 digest；然后在 SQLite 事务中以 `expected_layer_revision`、`expected_layer_digest` 和 `expected_active_revision` 执行 CAS。API/UI 修改同样必须携带 expected revision，默认只写明确指定的 SQLite 层；只有显式声明 `scope=user_source` 时才通过 source writer 原子替换用户级 JSONC，再由各 Workspace fan-out 导入。任何路径都不得通过诊断接口写库。文件候选与数据库最新 revision 无法证明基于同一基线时，报告 conflict 并保留现状。

这样既保持 `sqlite-config-storage` 的持久化边界，又修复“迁移后改 JSONC 无效”的问题。单纯重启不会绕过冲突、校验或 pending 状态。

首次迁移只在 SQLite 层记录不存在时执行；诊断重新读取只做观测，不触发迁移、备份或写库。备选方案是迁移后永久忽略 JSONC；它与用户可编辑配置文件的直觉和本次 VS Code 对齐目标冲突，因此不采用。另一备选是把 JSONC 重新设为唯一权威；这会破坏现有 SQLite 状态和事务约束，也不采用。

Workspace 的公开 `PATCH /api/v1/config` 当前只提供 runtime override 这一种可写 SQLite layer，因此请求必须显式携带 `config_layer=runtime_override`、`scope=workspace`、`base_layer_revision`/`base_layer_digest`、`expected_active_revision`/`expected_active_digest` 和调用方稳定的 `idempotency_key`。初始不存在的 layer 或 active snapshot 用成对的 `null` 表示；两个 CAS 对必须完整提供，不能只给其中一个。相同 `(config_domain, idempotency_key)` 只能绑定同一个 candidate；重试必须复用已完成的 active/pending 结果，不得新增 active revision 或重复同结果事件。source layer 与 active snapshot 的校验在同一个 `BEGIN IMMEDIATE` 事务中执行，任何一项不匹配都返回 `409 conflict`，并附带当前 revision 与来源摘要。用户级 JSONC 的物化不复用该入口，必须由显式 source-writer 完成。

### 3. 用集中策略表声明生效范围

新增内部配置生效策略注册表，至少支持：`immediate_read`、`next_job`、`next_session`、`restart_workspace`、`restart_gateway` 和 `rejected`。`restart_required` 只作为应用结果/状态，不是策略名称。策略表使用 JSON Pointer 精确匹配；对象通配符规则必须显式声明，数组只有声明 identity key 才能按元素匹配，否则按整个数组路径匹配。更具体路径优先，同优先级重叠规则在启动/构建时失败；schema 合法但未登记的路径按配置域默认到 `restart_workspace` 或 `restart_gateway`，schema 未知字段仍拒绝。策略表是唯一事实来源，schema 注释、诊断和测试由它生成或校验一致。配置事件的 `activation_scope` 使用 `current`、`next_job`、`next_session`、对应的重启范围；同一候选包含多个非重启范围时使用 `mixed`，旧记录或无法判断时使用 `unknown`，不得让客户端从布尔 `restart_required` 反推范围。

Workspace 的基线分类为：`llm`/`agents`/开发运行参数对后续 Job 生效，`default_agent` 对后续会话生效，UI/按请求读取值即时影响后续读取，MCP/logger 以及启动时捕获的超时、辅助服务和 Gateway 连接参数需要 Workspace backend 重启。Gateway 的 UI、目录调度和健康检查参数优先设计为进程内更新；工作区注册、SSH 隧道和服务变化通过运行时控制器的原子对账处理，其余字段进入 Gateway restart required。

备选方案是只按顶层 section 分类；它会把可热更新与单例缓存字段混在一起，正是当前行为不透明的来源，因此不采用。

### 4. Gateway 重载复用生命周期与所有权边界

Gateway 重载器只负责产生候选和调用受控应用器。registry 目标具有不可变 target id、owner 类型和 runtime lease，owner 只能是 `config`、`manual`、`system` 或 `remote_projection`。工作区注册变更交给 registry reconciliation；只对账 `config` 目标，不能删除其他 owner。服务地址或隧道变更必须持有 runtime lease，并在没有活动请求/流之后完成 drain；需要重启的 Workspace 交给已有 Gateway lifecycle controller，需要重启 Gateway 的候选只进入 pending 状态。

Workspace pending 候选归 Workspace 配置域所有。Gateway 只能调用 Workspace lifecycle/API，传递不透明的 `candidate_ref` 并接收 prepare、generation health proof 和 promotion 结果；Gateway 不得打开、查询或修改 Workspace `.boxteam/` 下的 SQLite、候选 payload 或秘密。远程场景再增加一层同样的委托：本地 Gateway 将 candidate ref 传给远程 Gateway，不能绕过远程 Workspace owner。

远程 Gateway 的配置和重启由远程拥有者处理，本地只接收结果并更新投影。这避免本地 Gateway 在联邦场景下直接操作远程工作区文件或连接。

### 5. 变化事件和诊断分离

内部配置变化事件使用独立的配置事件存储和订阅接口，不能复用按 Job 分区的 Job 事件总线；诊断 API 用于查询完整状态和最近错误。诊断接口重新计算来源摘要时不得隐式提交候选，避免“查询接口”改变运行时行为。

事件与诊断共同使用 `domain`、唯一 `event_id`、域内单调 `cursor`、`commit_revision`、active/pending revision、effective/layer/candidate digest、`changed_paths`、`applied_paths`、`deferred_paths`、`result`、`activation_scope`、`restart_required`、`source` 和 `error` 字段，确保 UI 不需要从布尔值推测实际状态。若整体候选进入 pending 或失败，`applied_paths=[]`；进入 pending 时 `deferred_paths=changed_paths`，不允许把可热更新子路径列为已应用。

### 6. 明确 revision、digest 和 pending 持久化模型

每个配置域维护一个单调递增的 `commit_revision`，它只表示持久化提交顺序；active/pending 记录分别引用自己的 commit revision。`effective_digest` 是规范化完整 active 配置的内容摘要，`layer_digest` 是每个 JSONC 层稳定内容的摘要，`candidate_digest` 是候选内容摘要，均不可作为事件顺序。事件另有独立 `event_id` 和域内 `cursor`。

pending 记录至少包含：`state`、完整规范化候选、来源层 key、来源路径、`base_active_revision`、每个来源的 `base_layer_revision`/`base_layer_digest`、candidate/effective digest、创建时间、最近错误、应用 generation 和持久化位置。pending 由对应配置域 SQLite 事务保存；原始 JSONC 仍保留并作为文件来源的可核验输入。

对 Workspace，`source layers / desired state` 是 Workspace SQLite 中按 `layer_key` 分开的记录；`active snapshot` 是旧 generation 唯一可读的运行时快照；`pending candidate` 是带 `candidate_id` 的完整、不可变、脱敏候选。JSONC 导入成功后，来源层记录可以立即更新为新 desired payload，但 active snapshot 不变；若策略要求重启，则同一事务额外写入 pending candidate 和 pending commit revision。这样“source 已更新”不等于“runtime 已激活”。新 generation 必须拿到绑定 `workspace_id`、`config_domain`、`candidate_id`、candidate revision/digest 和 generation fencing token 的不透明 `candidate_ref`，再通过 Workspace-owned 启动读取路径从同一 SQLite 读取 pending 完整候选；缺失、过期或 digest 不匹配时必须启动失败，禁止静默回退到 active。没有 candidate_ref 的旧 generation 只能读取 active snapshot，不能发现或采用 pending。

candidate payload 的秘密字段只保存 `secret_ref`、secret version、`secret_binding_digest` 或用户显式写入的自包含字面量 key；解析后的 provider API key 只在进程内由 Workspace-owned secret resolver 短暂使用。字面量 key 按原文写入 SQLite 以支持原样重启恢复（例如本地模型 dummy key、临时 apikey），其 binding digest 基于字面量原文，因此无法检测轮换；只有旧版本写入的不可逆 `literal-sha256:` 摘要才使候选 `rejected`，旧 active 保持不变。candidate/effective digest 基于规范化的 secret reference、binding digest 或字面量原文，而不是运行时新解析出的秘密字节。outbox、诊断、日志、健康证明和 Gateway 委托只允许出现 secret reference、是否存在和不可逆摘要，不得出现秘密原文或完整候选 payload。

pending 的处理规则固定为：来源基线未变时重启后尝试激活；来源只发生不重叠变化时重新生成候选并替换 pending；重叠变化进入 conflict；显式 discard 进入 discarded 并抑制同 digest 的重复 watcher 事件；副作用无法恢复进入 recovery_required。所有规则都必须保留可重试/可诊断记录。`retry` 沿用原 candidate 和逻辑 `idempotency_key`，只产生新的 attempt/apply claim；它不能产生第二个候选或第二个同结果配置事件。

### 7. SQLite 事务与外部副作用采用两阶段应用

SQLite 事务不能回滚 MCP 会话、logger、Workspace/Gateway 进程、SSH 隧道或已有代理连接。因此应用流程先持久化已校验候选和 `applying`，再执行 prepare/apply；只有所有必要副作用确认成功后，才在 SQLite 事务中 promotion active 并写入成功事件。

优先使用旧 runtime 仍存活的切换方式：新 generation 健康后再释放旧 generation；若 apply 失败，执行补偿并恢复旧进程、旧连接和旧 active。若旧 runtime 已停止且恢复失败，状态必须为 `recovery_required`，记录受影响资源、旧 active revision、pending revision 和人工/自动恢复动作；不得把停机报告为新配置已生效。

### 8. 配置事件流的存储、重放和去重

配置事件采用每个 Gateway/Workspace 配置域独立的 append-only outbox 记录，使用域内 cursor 排序。状态记录、revision、候选状态和 outbox event 必须在同一 SQLite 事务中提交；relay 只读取已提交 outbox，发布成功后再以 event_id/cursor 幂等标记。订阅支持 `after_cursor` replay；cursor 超过保留窗口时返回 `snapshot_required` 和当前快照摘要。生产者必须携带或派生稳定的调用方 `idempotency_key`，并以 `(domain, idempotency_key)` 绑定同一个 candidate；`attempt_id` 只表示一次实际应用尝试，不能作为恢复重试的逻辑提交身份。消费者以 event_id 去重。候选解析期间不发布成功事件；active promotion 或 pending/rejected/conflict/discarded/recovery_required/restart_failed/apply_failed 状态事务提交后才进入 outbox。同一 candidate 的 retry 不重新分配 commit_revision，也不重复发布相同结果的配置事件；只有状态确实从失败转为 active/pending 等新状态时才产生新的状态事件。

### 9. Gateway registry 对账的所有权与生命周期

配置对账先生成完整目标 batch，再按 owner 和稳定 target id 校验增删改集合；`manual`、`system` 和 `remote_projection` 目标不可被本地 config 候选删除。Gateway 配置版本从现有 `config_version: 1` 迁移到要求 `connection_id` 的新版本；`gateway.jsonc` 的 `workspaces` 每个元素必须有持久的不可变 `connection_id`。它是配置元素和 registry `config` target 的身份，不得使用数组下标、host、username、key 或它们的拼接推导。旧版本没有该字段时只在一次显式迁移中为每个无歧义元素生成并持久化随机 ID；后续加载不得重新生成，重复 ID 必须拒绝。保留同一 `connection_id` 时，host/username/key 变化表示同一目标的 generation 更新，先建立新连接并完成健康确认，再排空旧 generation；元素删除才表示删除该 config target。对需要替换的 config 目标保留旧 generation，等待全部 runtime lease、HTTP/SSE/WebSocket 使用者排空后再关闭隧道。远程投影只能由远程 Gateway 的有序事件或快照改变，并保留远程 cursor；断档时暂停破坏性对账。

实现上，远程 Gateway 连接记录也必须携带 `source_owner=config|manual`。配置对账只提交 `config` 连接和对应的 `remote_projection` targets；人工添加的连接、投影和 runtime 不进入配置删除集合。连接 ID 与 source owner 发生冲突时拒绝 batch，不能通过一次 `upsert` 把人工连接改成配置连接，或反向越权。配置 batch 先完成逐连接 prepare/健康探测，再一次性执行 registry revision CAS；旧 tunnel 只有在新 projection 提交后、且 route lease 已排空时才关闭。

远程 Gateway 的 federation manifest 必须额外返回不透明配置状态摘要：域内 `config_event_cursor`、`config_reload_state`、`config_reload_restart_required` 和可选 `config_reload_candidate_ref`。摘要不得包含候选 payload、secret 或健康 proof 原文；本地只把 candidate ref 当作委托句柄。`RemoteGatewayConnection` 和每个 `remote_projection` target 持久化最近已应用的远端配置 cursor。完整 workspace 快照是 cursor 的同步边界：收到比本地 cursor 小的快照必须拒绝并保留旧 projection；cursor 跳跃只能由完整快照重同步接受，不能把 digest 当作事件身份，也不能在快照成功前删除旧 projection。

### 10. Applying claim、generation fencing 与崩溃恢复

每个配置域同一时间只允许一个 applying claim。claim 是 SQLite 条件更新：要求目标仍处于 `candidate_validated` 或可应用的 `pending_restart`、active/pending revision 与 candidate_id 匹配，并写入 `apply_id`、attempt_id、owner、target generation、lease expiry 和 fencing token。watcher、API/UI、手动重启和启动恢复都必须通过同一 claim；抢不到 claim 的调用返回 busy/conflict 并重新读取状态。

所有外部副作用调用都携带 fencing token；promotion、失败、补偿和 outbox 写入再次校验 token，旧 apply 即使延迟恢复也不能覆盖新状态。lease 超时或进程崩溃后，启动恢复根据 runtime health proof 判断：旧 generation 健康且新 generation 无匹配证明时清除失效 claim 回到 pending/旧 active；新 generation 有完整匹配证明时进入受控 promotion；两者都无法证明时进入 `recovery_required`。

### 11. Pending runtime 的启动与健康证明

重启或新 generation 启动不是只传“使用最新配置”的隐式行为，而是显式传入不透明 `candidate_ref`。该 ref 至少绑定 `workspace_id`、`config_domain`、`candidate_id`、pending commit revision、candidate/effective digest、目标 generation、fencing token 和过期时间；Gateway 只转发 ref 和生命周期参数，不携带候选内容，也不能用 ref 反查 Workspace SQLite。Workspace launcher/控制 API 负责把 ref 传入新 backend（实现上可映射为本地受保护的启动环境或一次性内部参数），新 backend 再在启动时调用 Workspace-owned candidate loader，从 Workspace SQLite 精确读取该 `candidate_id` 的 pending payload。

candidate loader 的选择规则是硬性的：有 candidate_ref 时只读取且只允许匹配的 pending candidate；没有 candidate_ref 时只读取 active snapshot；candidate 不存在、状态不是可启动的 pending、绑定 revision/digest/token 不匹配或 secret resolver 失败时直接启动失败，绝不回退到 active。旧 generation 从不读取 pending，因此 JSONC 导入更新 source layer 或写入 pending 不会改变正在服务的进程。

Workspace backend 的健康接口必须回显实际加载的配置域、`loaded_source`（`active` 或 `pending`）、candidate_id（active 时为 null）、loaded commit revision、effective/candidate digest、secret binding digest、generation id 和 fencing token 摘要。Gateway/Workspace lifecycle controller 只有在回显与 pending 记录完全匹配、且每个必要 runtime consumer 的健康/lease 检查均成功后，才执行 promotion；HTTP 200 或普通 health=true 不构成配置加载证明。

旧 generation 的 process handle、连接 lease 和 active snapshot 在新 generation 获得匹配证明前保留。新 generation 启动失败且旧 generation 仍可用时回滚新 generation 并报告 `restart_failed`；旧 generation 已停止且无法恢复时写入 `recovery_required`，不把停机标记为新配置 active。promotion 只由 Workspace 配置域事务完成，Gateway 不能直接修改该事务。

### 12. Gateway runtime 应用流程矩阵

| runtime 类别 | prepare | apply / 健康确认 | promotion | rollback / 失败结果 |
| --- | --- | --- | --- | --- |
| UI/配置读取 | 校验快照和消费方版本 | 下一次请求读取新 snapshot，无外部副作用 | active promotion 后可见 | 读取切换失败则恢复旧 snapshot，`apply_failed` |
| 目录/Generator scheduler | 构造新调度参数和 scheduler generation | 在调度边界切换；回报 scheduler generation 和参数摘要 | 新周期确认后 promotion | 保留旧 scheduler；`apply_failed` 或 `recovery_required` |
| health controller | 校验 timeout/poll 参数 | 在下一轮探测切换；回报 controller generation | 新轮次成功后 promotion | 恢复旧参数/控制器；失败进入 `apply_failed` |
| registry batch | 生成完整目标 batch，校验 owner/id/lease | 原子应用 config owner；目标健康且 lease 可转移 | batch 全部确认后 promotion | 反向 batch；不能恢复则 `recovery_required`，不动 manual/system/projection |
| SSH tunnel/proxy | 建立新认证连接和 runtime lease | 完成握手；等待旧 lease/请求/SSE 排空 | 新 tunnel 可服务后转移 lease | 保留旧 tunnel；无法恢复旧 tunnel 则 `recovery_required` |
| Workspace backend process | 生成带 candidate_id/revision/digest 的启动契约 | 健康响应必须返回匹配 loaded proof | Gateway 健康确认后 promotion | 新进程失败恢复旧进程；否则 `recovery_required` |
| Gateway process | 由 supervisor 基于 active snapshot 或匹配 restart intent 生成 Gateway candidate_ref，预留旧 generation/监听资源 | 新 Gateway 只能由 Gateway-owned loader 读取匹配 pending，并返回 gateway generation、candidate/revision/digest、secret binding 和 fencing proof；普通崩溃恢复不得自动加载 pending | proof、所有 Gateway runtime consumer、最终 source/registry CAS 全部通过后切换 active generation 和 listener | 新进程失败时放弃新 generation 并保留/恢复旧 Gateway；旧新均不可用则 `recovery_required` |
| remote projection | 校验 remote owner、cursor 和快照 | 只接受连续 cursor 或完整快照 | 投影 batch 完成后更新 cursor | 保留旧投影；断档/离线为 blocked，不做破坏性删除 |

Gateway 的热应用消费者必须返回可执行的 rollback 句柄；reload 只有在该句柄准备好并完成所有外部 apply 记录后才允许进入 active promotion。若最终 source/active/registry CAS 失败，先将 apply journal 转入 `recovery_required`，调用同一 apply 的 rollback；回退成功后 journal 进入 `compensated`、候选进入 `conflict`/`apply_failed`，回退失败则保留 `recovery_required` 和逐资源错误。Gateway pending promotion 则把 `healthy/reserved` 的新 runtime generation、旧 `active/serving` generation 的 draining 标记、restart intent、active snapshot、pending candidate、apply journal 和成功事件放在同一个 SQLite 事务中；事务失败时新 generation 只能关闭，旧 generation 仍 serving。

每个 consumer 阶段还必须绑定当前 apply claim 的 fencing token 摘要，并在 prepare、apply、health、promotion 和 rollback 前重新检查 claim。claim 已过期、被新 apply 替换或 token 不匹配时，旧阶段不得继续修改运行时；若已有副作用且无法在当前 claim 下安全补偿，必须停止 promotion 并进入 `recovery_required`，不能只依赖最后一次 active CAS。

Gateway pending 启动期间允许恢复流程为 registry 写入启动期状态（例如恢复托管 Workspace 的连接错误或 runtime lease），但这类写入必须带 `system`/`config_batch` owner，并追加连续的 registry apply journal。Gateway 在生成最终 health proof 前，必须以 pending apply 保存的 registry 基线重新读取当前 revision；只有能够验证从该基线到当前 revision 的 journal 全部连续且属于启动期 owner 时，才可 CAS 地 rebase apply 基线。任何 `manual`/`manual_crud`、remote projection 或缺失/不连续 journal 都是并发冲突，必须阻止 promotion 并按最终 CAS 失败路径处理。

这里的 mutation owner 与 target owner 是两个边界：启动恢复可以由 `system` mutation 更新已存在的 `manual`、`config` 或 `remote_projection` target 的运行时字段（例如 backend 地址、连接错误和 runtime lease），但不得创建、删除、改写 target owner、稳定 identity、namespace 或配置来源字段；该写入仍必须带 expected registry revision 并记录 `system` journal。这样恢复运行时状态不会越权改变人工/配置/远程投影的所有权，也不会把人工 CRUD 的 revision 冲突变成静默覆盖。生命周期 owner 为 `system` 的默认 Workspace 另有一个受限的用户展示字段例外：用户可以通过 manual CRUD 修改名称或父级组织，但不能修改其 root、backend、managed/removable、stable identity 或 owner。

### 13. 配置来源与权威存储矩阵

| 修改入口 | 来源层/owner | 权威存储与 payload | expected CAS / digest | 配置事件 | 生效边界 |
| --- | --- | --- | --- | --- | --- |
| 用户级 `gateway.jsonc` | Gateway `user` layer | Gateway SQLite；用户级规范化 payload | `user.layer_revision` + `user.layer_digest` + active revision | 是 | 按 Gateway 策略 |
| 用户本地 `gateway_local.jsonc` | Gateway `user_local` layer | Gateway SQLite；本机覆盖 payload | `user_local.layer_revision` + `user_local.layer_digest` + active revision | 是 | 按 Gateway 策略；只在本机 |
| 用户级 `workspace.jsonc` | Workspace `user` layer | 每个 Workspace 自己的 SQLite；同一用户源的 materialized payload，带 `source_generation`/`fanout_id` | 目标 Workspace 的 `user.layer_revision` + `user.layer_digest` + active revision；不使用其他 Workspace 的 revision | 是，按 Workspace 域分别产生 | 按 Workspace 策略；可影响多个 Workspace |
| 用户本地 `workspace_local.jsonc` | Workspace `user_local` layer | 每个 Workspace 自己的 SQLite；本机覆盖 payload | 目标 Workspace 的 `user_local.layer_revision` + `user_local.layer_digest` + active revision | 是，按 Workspace 域分别产生 | 按 Workspace 策略；只影响本机 |
| 当前工作区 `.boxteam/workspace.jsonc` | Workspace `workspace` layer | 当前工作区 `.boxteam/state/workspace.sqlite`；工作区 payload | `workspace.layer_revision` + `workspace.layer_digest` + active revision | 是 | 只影响当前工作区 |
| Workspace runtime override API/UI | 明确指定 `user`/`user_local`/`workspace` 的 config layer | 目标 Workspace SQLite；API 不得隐式改写其他层 | 指定 layer revision + digest + active revision；scope 必须显式 | 是 | 后续 Job/会话或重启 |
| 既有会话 Agent/model/tool 设置 | session state | 会话 manifest/状态存储 | session revision | 否，产生会话事件 | 当前会话或后续 turn |
| Gateway 用户 UI 视图设置 | user view state | Gateway user-view SQLite | view revision | 否，产生用户视图事件 | 当前用户视图 |
| Gateway registry CRUD | registry owner=`manual` | Gateway registry SQLite；目标含不可变 `connection_id`/target id | registry revision + immutable target id | 否，产生 registry 事件 | registry 控制面 |
| remote projection | owner=`remote_projection` | 本地 Gateway projection/registry 状态 | remote cursor + target id | 否，产生 federation 事件 | 由远程 owner 决定 |

用户级 `workspace.jsonc` 的一次稳定文件变化生成一个 `fanout_id`，由显式 source writer 产生；直接人工编辑没有 writer metadata 时，watcher 使用规范化 source path 与 post-write `layer_digest` 派生稳定的 fan-out 关联键。`fanout_id` 只用于关联和幂等，不是 commit revision、event cursor 或内容摘要的替代物。每个 Workspace watcher 或显式 fan-out coordinator 独立导入：运行中的 Workspace 立即尝试，未运行的 Workspace 在下次启动时按已记录的 source generation/digest 追赶。每个 Workspace 独立执行自己的 layer/active CAS，并分别产生 `candidate_id`、事件和结果；跨多个 Workspace 没有 SQLite 全局事务。部分成功返回 `fanout_partial` 诊断汇总状态，该名称不是单 Workspace 配置事件的 `result` 枚举；成功 Workspace 不回滚。单个 Workspace 与本地 API 修改冲突时只在该 Workspace 进入 `conflict`，其他 Workspace 继续按自己的基线处理。被更高优先级层遮蔽时仍更新该层 revision/digest，但 effective digest 不变，结果为 `unchanged`。

API/UI 只有在明确声明修改 config layer 和 scope（单 Workspace 或用户级 source）时才进入配置事件流；用户级 source 写入先原子替换 JSONC，再由 fan-out 导入，不直接写多个 Workspace SQLite。诊断接口永远不在上述矩阵中作为写入入口。

### 14. 状态、结果、revision 与 candidate_id 对应关系

| 状态/结果 | candidate_id / attempt_id / apply_id / idempotency_key | commit_revision | active revision | applied/deferred | 说明 |
| --- | --- | --- | --- | --- | --- |
| `candidate_validated` | 必有 candidate/idempotency；尚未 claim 时 `apply_id=null` | 未分配 active commit；若已预留 pending revision 可追溯 | 不变 | 空/空 | 仅表示候选完成校验 |
| `applied` + `active` | 四者必有 | 有，单调递增且只为逻辑 candidate 分配一次 | 指向该提交 | changed/applied，deferred=[] | `activation_scope` 为 current/next_job/next_session/mixed |
| `restart_required` + `pending_restart` | candidate/idempotency 必有，未应用时 `apply_id=null` | 有，指向 pending；不是 active revision | 仍指向旧 active | `applied=[]`，deferred=全部 changed | 不得部分应用 |
| `restart_failed` | candidate/attempt/apply 必有 | pending 的 revision | 旧 active | `applied=[]`，deferred=全部 changed | 旧 runtime 已恢复；同 candidate retry 不新建 commit |
| `apply_failed` | candidate/attempt/apply 必有 | 若候选未持久化则为空，否则沿用 pending revision | 不变 | `applied=[]`，deferred=[] 或 pending 全量 | 热应用补偿成功 |
| `rejected` / `conflict` | candidate/attempt/idempotency 必有，`apply_id` 可空 | 为空（没有有效配置提交） | 不变 | `applied=[]`，`deferred=[]` | 失败尝试不递增 commit_revision |
| `discarded` | candidate/attempt/idempotency 必有 | 原 pending revision 可追溯 | 不变 | `applied=[]`，`deferred=[]` | 用户显式丢弃 |
| `recovery_required` | candidate/attempt/apply 必有 | 关联 pending 或失败尝试 | 旧 active 或不可服务 | 不得声称 applied | `retry` 使用新 attempt/apply，但沿用 candidate/idempotency |
| `unchanged` | candidate/attempt/idempotency 必有，`apply_id=null` | 不变 | 不变 | 不产生配置事件 | 仅作为 API/诊断结果；被遮蔽的 source layer 可有独立同步记录 |

`event_id` 和域内 cursor 始终独立于上述 revision/digest；事件在同一 SQLite 事务写入 outbox，relay 失败不影响状态可回放。

### 15. Gateway 自身重启必须有独立的 pending 启动协议

`restart_gateway` 不复用 Workspace backend 的 candidate loader，而是使用 Gateway 控制面自己的持久化协议。Gateway 控制面 SQLite 至少维护以下逻辑记录：

- `gateway_active_snapshot`：以 `config_domain` 为唯一键，保存当前 active revision、完整脱敏规范化 payload、所有 source layer 基线、effective/secret binding digest、已确认的 runtime generation 和 promotion apply_id。
- `gateway_pending_candidate`：以 `(config_domain, candidate_id)` 为唯一键，保存 pending candidate、source 基线、candidate revision/digest、目标 generation、状态和持久化位置。
- `gateway_runtime_generation`：以 `(config_domain, generation_id)` 为唯一键，保存进程身份、二进制/配置版本、加载来源、health proof、fencing token、监听句柄和当前状态。
- `gateway_restart_intent`：以 `intent_id` 为唯一键，保存用户确认或恢复操作绑定的 candidate_id、pending revision、全部 source 基线、target generation、fencing token、过期时间、申请者和状态。
- `gateway_apply_claim`：保存当前 apply_id/attempt_id、owner、candidate_id、base revision、target generation、lease/deadline 和 fencing token。

普通崩溃恢复与用户确认的 pending 应用必须使用不同的启动路径。没有未过期 `gateway_restart_intent` 的普通崩溃重启只能加载 `gateway_active_snapshot`，必须保留 `pending_restart`，不能因为 SQLite 中存在 pending 就自动采用它。用户确认应用时，确认操作与 intent、apply claim 的创建必须在同一 SQLite 事务中完成；启动器只接受与当前 pending、source 基线和 fencing token 完全匹配的 intent。正在恢复一个已进入 `applying` 的旧 attempt 时，也只能依据 durable claim 和已有 generation proof 恢复，不能把普通重启伪装成新的用户确认。

Gateway 进程本身由稳定的 supervisor/launcher 管理。supervisor 必须在新 Gateway 获得匹配 proof 前保留旧进程句柄、旧监听资源或可重启旧 active snapshot 的启动材料；需要端口交接时使用受控 socket handoff，不得先杀旧进程再尝试启动新进程。新 Gateway 的启动契约只携带不透明 `gateway_candidate_ref`，由 Gateway-owned loader 读取本 Gateway SQLite 中匹配的 pending candidate；ref 绑定 `gateway_id`、config domain、candidate_id、pending revision、candidate/effective digest、target generation、fencing token 和过期时间。`gateway_restart_intent.gateway_id` 必须由创建 intent 的 Gateway 写入，pending loader 在启动时以本机 `identity.json` 得到的 Gateway id 做 CAS 身份校验；旧版本 intent 没有该绑定时不得自动采用 pending，必须显式重新生成 intent。

当受控重启必须先让旧进程释放监听时，旧 Gateway 收到 supervisor 的 graceful shutdown 不能把仍绑定该 pending intent 的旧 generation 直接标记为 `closed`；它必须以 CAS 转为 `state=active/listener_state=draining`，保留旧 active snapshot 和恢复身份，直到新 generation promotion 成功或 supervisor 明确执行回退。没有匹配 pending intent 的普通停止才关闭当前 generation。这样新 Gateway 可以用 `intent.old_generation` 校验旧 active，启动失败时 supervisor 仍能从该 generation 或 active snapshot 恢复。

新 Gateway 的 health proof 必须回显 `gateway_id`、generation_id、`loaded_source`、active/pending revision、candidate_id、effective/candidate digest、secret binding digest 和 fencing token 摘要，并携带六类 Gateway runtime consumer 的脱敏 proof digest：`catalog-generator-scheduler`、`health-controller`、`registry-batch`、`ssh-tunnel-proxy`、`workspace-process` 和 `remote-projection`。digest 只证明对应 consumer 的 generation、状态、参数摘要和 fencing token 摘要，不能替代 consumer 自身的可执行健康检查，也不能保存地址或秘密。supervisor 只有在 proof 匹配、全部 Gateway runtime consumer 通过健康/lease 检查且 promotion 事务的最终 source/registry CAS 成功后，才能切换 active。新进程失败时，旧 Gateway 仍存活则放弃新 generation、保留旧 active 和 pending 并记录 `restart_failed`；旧进程已退出时，supervisor 必须先按 active snapshot 恢复旧 generation，恢复也失败才进入 `recovery_required`。任何缺失或不匹配的 proof 都不能通过普通 HTTP healthy 代替。

如果 pending candidate 在 Gateway-owned loader 的 schema 校验、secret binding 或其他运行时初始化之前就失败，启动边界必须执行同一套 owned failure transition，而不能因为 `GatewayConfigReloadService` 尚未创建就丢失恢复记录。该 transition 只有在 `gateway_id`、target generation、fencing token 与未过期 restart intent 全部匹配时才允许把 intent/candidate/apply journal 置为 `recovery_required` 并释放 claim；过期 intent、身份不匹配或 stale generation 的启动尝试必须原样保留 pending，等待显式 retry。

该 failure transition 由 GatewayStateStore 在一个 `BEGIN IMMEDIATE` 事务中完成：先锁定并校验 intent、pending candidate、现有 apply claim 的完整启动契约，再一起更新 recovery 状态、apply journal、恢复事件 outbox 和 claim。事件沿用 pending candidate 的 `idempotency_key`，重复的同契约失败回报只能复用已有事件；任何 generation、fencing、Gateway identity、intent 时效或 intent/candidate 状态配对不匹配都在写入前拒绝。

Gateway supervisor 的监听交接由 Launcher 实现为稳定 public listener 加私有 child listener：public listener 只在 supervisor 启动时绑定正式 Gateway 地址，每个 Gateway generation 由 supervisor 分配独立的本机临时端口，健康检查和 candidate proof 完成前不暴露该端口。handoff 请求通过 `${BOXTEAM_HOME}/state/gateway-supervisor.sock` 的受保护 Unix socket 发送；当路径超过 Unix socket 系统上限时，使用由绝对 `BOXTEAM_HOME` 的 SHA-256 派生、位于系统临时目录的短路径，客户端和服务端必须使用同一派生规则。控制消息只包含 `candidate_ref`、目标 generation 和 fencing token，不包含候选 payload 或秘密。

交接顺序固定为：

1. supervisor 保留旧 child 和 public target，串行化 handoff 请求并验证三元启动契约；
2. supervisor 以 pending 环境启动新 child 到私有端口，Gateway-owned loader 从同一 SQLite 读取匹配 intent/pending，并等待 HTTP ready；
3. 新 child 完成六类 Gateway consumer 检查、pending health proof 和 promotion CAS 后，supervisor 将 public target 原子切换到新私有端口；
4. 只有切换成功后才向旧 child 发送 graceful shutdown；旧 Gateway 的 Workspace runtime handle 在切换期间可被新 generation 复用，旧 child 退出时不得再次关闭这些已接管的进程；
5. 新 child 启动失败或 proof/promotion 被拒绝时，supervisor 保持 public target 指向旧 child，pending 和旧 active snapshot 不变，并返回明确的 `restart_failed`；若旧 child 已不可用，则按 active snapshot 启动无 pending 环境的 fallback，fallback 也失败才进入 `recovery_required`。

普通 Gateway 崩溃恢复不经过 handoff socket，也不携带 candidate 启动契约；它只能加载 durable active snapshot。用户确认 pending 应用则由 Gateway 创建并绑定 restart intent，再由开发重启 helper 将契约交给现存 supervisor；没有现存 supervisor 时，Launcher 先启动 pending child，pending child 失败才启动 active fallback。Gateway 新旧 generation 共享同一个 Gateway SQLite 时启用 SQLite WAL 和数据库级事务锁，但仍保留默认单进程 ownership lock；只有 supervisor 明确管理的 successor handoff 才允许该共享模式，不能让普通业务进程绕过单进程保护。

### 16. Promotion 前必须执行最终 source CAS

候选创建时保存的 source 基线只是 prepare 阶段的快照，不是 promotion 授权。promotion 前，配置域必须在同一个 SQLite 事务中重新读取并比较所有参与候选的 source layer：每层的 `layer_revision`、`layer_digest`、`presence`，共享 source 的 `source_generation`，以及 Gateway registry 候选涉及的 `registry_revision`。比较集合必须是完整来源集合，不能只比较发生变更的路径。

最终 CAS 失败时，候选不得成为 active，也不得递增 active commit revision 或写入 applied paths。尚未产生外部副作用时，释放 claim 后重新生成候选；能够证明变化不重叠时基于最新 source 重建，无法证明时进入 `conflict`。已经产生外部副作用时，必须先停止 promotion 并按 apply journal 执行补偿；补偿成功保留旧 active 和可重试 pending，补偿失败进入 `recovery_required`。source owner 在 apply 期间的任何新提交都必须递增 source generation，即使新内容最终与旧内容相同也不能复用旧事件身份。

active snapshot、source layer、registry batch、promotion 状态和配置 outbox 的最终 CAS/更新必须属于同一配置域事务。事务提交前不得发布 active 成功事件；事务提交后即使 relay 或进程崩溃，也必须能从 outbox 和 apply journal 恢复。这样可以阻止“候选 A 基于 source 1 开始 apply，source 已到 2 后仍把 A promotion 为 active”的覆盖路径。

apply journal 将外部副作用与 SQLite 状态分开记录：apply 阶段只追加脱敏的 `resource`/`action` 摘要，SQLite 回滚不会被解释为 MCP、logger、进程或 SSH 隧道已经回滚。外部补偿器必须在同一 `apply_id` 下提交 `status=succeeded|failed` 的补偿结果；补偿成功将 journal 置为 `compensated`，补偿失败保留 `recovery_required` 和错误摘要。任何 discard 或后续 promotion 都必须先通过该 journal 的状态 CAS，不能用“没有数据库改动”代替外部资源补偿证明。

### 17. JSONC 文件必须具有明确的 present/absent 语义

每个 JSONC source layer 记录必须有 `presence`，取值为 `present` 或 `absent`，并允许 `payload=null`。文件存在时，`present` 携带规范化 payload 和稳定字节 digest；规范文件被删除时，watcher 必须通过 source owner 写入新的 layer revision、`absent` tombstone、删除前 digest、canonical path 和 source generation。SQLite 中已有的旧 payload 不能继续代表已删除的文件，也不能因为启动优先返回 SQLite payload 而掩盖删除。

合并时 `absent` 表示该层不提供覆盖，结果向下落到更低优先级层；它与存在但内容为空的 JSONC 对象不同。删除后的 effective 配置变化仍必须经过完整候选、策略分类和 active/pending 判断；如果只是被更高层遮蔽，则 source layer revision 会变化而 effective digest 不变，结果为 `unchanged`。删除一个需重启层的有效贡献必须产生 `pending_restart` 或对应的 Gateway pending，不得直接清除 active。

watcher 只处理 canonical source path 的稳定事件。编辑器临时文件、`.tmp`、`.swp`、`.bak`、恢复文件和未完成 rename 不得单独导入；原子 rename 必须由 source journal 按顺序记录为旧路径 absent、新路径 present，或由显式 source writer 记录为一个有序操作。删除/rename 导入前后都要保存原始文件备份或可恢复的 digest 记录；恢复操作必须重新走 source owner 和 CAS，不能直接把备份内容写进 SQLite。

### 18. Active snapshot 必须是可恢复的持久记录

active snapshot 不能只存在于进程内对象。其最小记录结构固定为：

```text
config_active_snapshot
  PRIMARY KEY (config_domain)
  active_revision
  candidate_id nullable
  normalized_redacted_payload
  source_baseline_json
  source_generation
  layer_revisions_json
  layer_digests_json
  effective_digest
  secret_bindings_json
  schema_version
  promoted_generation
  promoted_apply_id
  promoted_at

config_pending_candidate
  PRIMARY KEY (config_domain, candidate_id)
  UNIQUE (config_domain, idempotency_key)
  pending_revision
  normalized_redacted_payload
  source_baseline_json
  candidate_digest
  effective_digest
  target_generation
  fencing_token
  state
  last_error
  created_at
```

实际表名可以不同，但必须保持上述唯一键和字段语义。active、pending、source layer 和 apply claim 必须可分别读取；pending candidate 不能通过覆盖 active 行来表示。promotion 事务同时更新 active snapshot、pending 状态、apply claim 和 outbox；事务提交前崩溃保留旧 active/pending，提交后崩溃由 outbox 和 active snapshot 共同恢复，不允许出现 active 已变但没有可重放事件的状态。

启动恢复总是先读取 `config_active_snapshot`。只有匹配的 restart intent/candidate_ref 才能读取 pending；pending 存在但没有授权 intent 时，普通崩溃重启继续使用 active 并保留 pending。active 记录缺失、payload/digest 损坏或 source baseline 无法核验时不得从 pending 猜测恢复，必须进入 `recovery_required` 或明确的首次 bootstrap 流程。source 已更新但 pending 尚未 promotion 时，active snapshot 保持旧 payload；promotion 已提交后进程崩溃时，恢复依据 active snapshot 的 promoted generation 和 fencing token，而不是重新读取当前 JSONC。

### 19. 用户配置保持 `api_key`，内部规范化为 `secret_ref`

本变更不把内部 `secret_ref` 直接暴露为当前用户 JSONC 的新必填字段；现有用户 schema 继续接受 `api_key`，包括受支持的 `${ENV_NAME}` 形式和自包含字面量 key。导入器在候选边界做规范化：环境变量表达式映射到不可逆的 env reference；字面量 key 按原文保留并直接作为运行密钥使用，因为它自包含、无需外部 resolver，可原样重启恢复。字面量 key 的唯一代价是无法通过 binding digest 检测轮换，且出于安全考虑，诊断、事件、日志、outbox 和 health proof 中只暴露其不可逆摘要。只有旧版本写入的 `literal-sha256:` 摘要无法还原成可用 key，导入直接返回 `secret_reference_required`，不写入 candidate、SQLite、outbox 或诊断。

旧 SQLite 中的 literal key 或 `${ENV_NAME}` 记录必须在迁移时逐条规范化。`${ENV_NAME}` 规范化为 `env:NAME` 引用，literal key 按原文保留，两者迁移成功都不阻断；仅不可逆 `literal-sha256:` 摘要迁移时保留阻断路径并把迁移状态置为 blocked/recovery_required，同时保留旧 active，不复制或回显该摘要之外的秘密值。secret rotation 只对 `env:` 引用有意义，会生成新的 binding/version 和 candidate digest，旧 active 继续服务直到新 generation 返回匹配 proof；resolver 失败时新 generation 直接失败，不能回退后声称 pending 已加载。将来若用户 schema 正式支持 `secret_ref`，必须另行提升配置版本并保留本边界的兼容读取规则。

### 20. `connection_id` 迁移必须是可恢复的版本流程

Gateway 配置迁移使用持久 `migration_id` 和以下状态序列，迁移前后的 schema 校验必须分开：

```text
legacy_read
  -> legacy_schema_validated
  -> migration_planned
  -> backup_written
  -> ids_persisted
  -> new_schema_validated
  -> registry_generation_created
  -> completed
```

`legacy_read` 先按旧 `config_version: 1` schema 校验原始文档，再生成每个无歧义 workspace 元素的 connection_id、expected legacy digest、new digest、backup location 和 migration plan。`backup_written` 必须在原子替换前完成；`ids_persisted` 与新 source digest、migration_id 一起写入 Gateway SQLite。只有新 schema 校验成功并且 registry generation 能以新身份建立后，才允许建立新的 active generation。每个 source layer 的迁移状态独立记录，但配置域不能在必需层仍处于 partial 时 promotion。

迁移写入必须使用保留注释的 source-preserving patch，不得通过无序 JSON 序列化丢弃注释；无法安全插入 connection_id 时在替换前失败。中途崩溃时，恢复器根据 migration_id、old/new digest 和 backup 状态选择完成 journal、重试同一写入或恢复原始字节；重复执行若 digest 和 ID 已匹配则幂等完成，不能生成新 ID。旧程序回滚由 supervisor 的版本感知 rollback 操作执行：先从精确 backup 恢复 v1 文档，再启动旧 binary，不能把 v2 文档交给不认识 connection_id 的旧 loader。

### 21. Fan-out 必须经过单一 source journal

共享的用户级 `workspace.jsonc` 由单一 source owner 维护 append-only journal，不能由每个 Workspace watcher 仅凭 path/digest 派生事件身份。source journal 至少包含 `(source_key, source_generation)` 唯一键、`source_event_id`、canonical path、presence、post-write digest、layer revision、前一 digest、writer/watch origin、时间和 fan-out 状态。source owner 以 CAS 串行提交新事件并单调递增 source generation；`fanout_id` 直接绑定 source_event_id/generation，digest 只用于判断内容是否重复，不承担事件身份或幂等身份。

因此 `A → B → A` 必须得到连续的 generation 1、2、3 和三个可追踪的 source event/fanout id；只有 `A → A` 且前后稳定快照相同才可以被 digest 去重。停止中的 Workspace 在启动时读取 source journal 的 high-water mark 和自身 last-applied generation，逐个追赶未处理事件；不能从当前文件 digest 猜测错过了哪些变化。每个 Workspace 仍在自己的 SQLite 中独立执行 layer/active CAS，source owner 不伪造跨数据库事务，fan-out 汇总只报告逐 Workspace 结果。

### 22. Registry batch、manual CRUD 和 promotion 必须共享 revision CAS

Gateway registry 维护一个 `registry_meta` 单行 revision 和 append-only apply journal。config batch、manual CRUD、system 更新和 active promotion 都必须携带 `expected_registry_revision`，在同一个 SQLite 事务中以 revision CAS claim；不得使用整表删除重插入作为对账提交。batch 记录 `batch_id`、base revision、desired batch digest、owner/target 操作、apply_id、fencing token、lease 和逐资源补偿结果。

目标身份使用明确命名空间：本地 Gateway 内 `connection_id` 在 `owner=config` 的配置元素集合中唯一；registry target key 还必须绑定 `gateway_id`、owner 和 target namespace；远程投影使用来源 Gateway id 加 remote target id，不得与本地 manual/config target 共享可变名称。manual CRUD 在 config batch 已 claim 或外部副作用执行期间提交时，必须因 registry revision 不匹配返回 conflict；config batch 也必须因最终 registry CAS 失败而停止 promotion并补偿，不能覆盖 manual 修改。恢复器依据 registry apply journal 继续未完成 batch 或恢复旧 batch，无法证明安全状态时进入 `recovery_required`。

## Acceptance Matrix

| 场景 | 输入/操作 | 预期结果 | 关键证据 |
| --- | --- | --- | --- |
| 文件/API 冲突 | 旧 JSONC digest 与旧 API revision 同时提交 | CAS conflict；active 不变；无 applied paths | conflict 事件、双方 revision/digest |
| 并发修改 | watcher 读取期间原子替换 JSONC | 重读或 TOCTOU conflict；不提交过期字节 | 文件前后 digest、SQLite 无部分写入 |
| 重复 watcher | 同一层同一 digest 产生多次事件 | 一次 commit_revision、一个 event_id | cursor、幂等记录 |
| 混合修改 | hot 字段与 restart 字段同一候选 | 整体 pending；`applied_paths=[]`；全部 changed paths deferred | pending 记录与事件 |
| 重启失败 | 新 generation 不健康 | 优先恢复旧进程；否则 `recovery_required` | process generation、旧 active/pending |
| registry 对账 | 配置删除与 manual/system/remote projection 并存 | 只删除 config owner；活动隧道先排空 | owner、target id、lease、batch 结果 |
| 远程委托 | 远程 Gateway 配置需要重启或离线 | 由远程 owner 应用；本地显示 pending/offline | remote cursor、委托结果 |
| 诊断只读 | 请求 sources/reload-status | 只读，不迁移、不备份、不写库 | SQLite revision/digest 不变 |
| pending 真实加载 | Workspace pending 候选启动新 backend | 新 backend 只能通过 `candidate_ref` 读取 Workspace SQLite pending，健康 proof 返回匹配 candidate/revision/digest；旧 backend 继续读取 active | 启动参数、candidate loader 选择、health proof、promotion 事务 |
| 用户级 Workspace fan-out | 一个 `workspace.jsonc` 变化，多个 Workspace 同时运行、停止或各自有本地修改 | 每个 Workspace 独立 CAS 和 revision/event；成功者不回滚，冲突者进入 conflict，停止者启动时追赶；跨 Workspace 不伪造全局原子性 | `fanout_id`、各 Workspace layer/commit revision、candidate/event/result |
| Gateway connection identity | 保持 `connection_id` 修改 host、username 或 key；另一个元素被删除；旧配置迁移产生 ID | 同 ID 原地更新并保留旧 generation 至新 tunnel 健康/排空；删除才移除 target；迁移 ID 持久且重复 ID 拒绝 | config payload、target id/owner、generation、lease、对账 batch |
| Gateway 自身 pending 重启 | 用户确认 `restart_gateway`，或 Gateway 普通崩溃后恢复 | 有 intent 才能按 candidate_ref 加载 pending；普通崩溃只加载 active；新 generation proof 不匹配则旧 generation/active 保持或恢复 | restart intent、Gateway candidate_ref、runtime generation、health proof、supervisor、fencing token |
| Gateway pending 身份/时效 | candidate_ref 被另一 Gateway 使用，或 pending intent 超过有效期 | Gateway-owned loader 必须校验持久 `gateway_id` 和 `expires_at`；缺失/不匹配/过期均拒绝 pending；显式 retry 为原 candidate 生成新的 generation/token/有效期 | intent gateway_id/expires_at、identity.json、拒绝原因、retry CAS、pending 状态未被错误提升 |
| apply 期间 source 二次修改 | 候选 A 基于 source 1 apply，source 更新到 2 后准备 promotion | promotion 前重新 CAS 全部 source layer/active/pending/registry 基线；失败不 active，按是否已有副作用停止、补偿或进入 recovery_required | 全量 source baseline、最终 CAS、apply journal、补偿结果、无 applied paths |
| JSONC 删除与 rename | 已迁移 JSONC 被删除、临时文件出现或路径原子 rename | 写入 absent tombstone 并按层合并；临时事件不导入；rename 有序记录 absent/present；需重启贡献保留旧 active/pending | presence、tombstone、source generation、备份/digest、changed/deferred paths |
| active snapshot 崩溃恢复 | source 已更新但 pending 未 promotion，或 promotion/outbox 后进程崩溃 | active 与 pending 分离；启动先用 active；提交后从 snapshot/outbox/journal 恢复，不从当前 JSONC 猜测；损坏进入 recovery_required | snapshot 主键/字段、promoted generation、fencing token、outbox cursor、恢复状态 |
| secret 兼容与轮换 | 用户使用 `api_key`、`${ENV}`、旧 literal SQLite 或轮换 secret | 公共 schema 保持 api_key，内部保存 reference/binding 或自包含字面量；旧 literal-sha256 摘要/resolver 失败不泄露；新 binding proof 成功后才 promotion | schema 版本、迁移映射、binding digest、脱敏 proof、旧 active |
| source A→B→A | 共享 source 连续写入 A、B、A，Workspace 中途停止 | source owner 分配 generation 1/2/3 和 distinct fanout id；digest 只做去重；停止 Workspace 从 high-water 追赶 | source journal、source event id、last-applied generation、fanout 结果 |
| registry/manual 并发 | config batch、manual CRUD、active promotion 交错提交 | 所有操作以 registry revision CAS 串行化；旧 batch 不能覆盖 manual；promotion 失败按 apply journal 补偿；命名空间隔离 | registry_meta revision、expected CAS、owner/namespace、batch journal、冲突/恢复结果 |

### 真实双进程验证边界

真实链路由 `tests/integration/gateway/test_gateway_launcher_handoff.py` 固化：Launcher 管理 Gateway generation，Gateway 通过 public listener 代理到独立 Workspace backend；测试先通过 Workspace API 修改配置，再编辑 Workspace JSONC，分别从 Workspace 与 Gateway 的独立配置事件接口按 cursor replay，创建后续会话并执行一个后续 Job，最后编辑 Gateway JSONC、加载 pending candidate 并完成 listener handoff。Job 使用仓库内本地 OpenAI 协议替身，不能推导真实外部 Provider 的兼容性、延迟或认证行为。

当前 Web 客户端没有 Workspace 配置编辑控件，因此本变更只把 API 修改作为 API/UI 配置入口的真实证据，不宣称浏览器专用配置编辑已验证。Windows、Docker/容器、真实外部 Provider 和平台级进程管理边界仍需各自的运行环境验收；它们不应被本地 Linux 替身或 focused tests 冒充通过。

## Risks / Trade-offs

- [Risk] JSONC 与 SQLite 同时被编辑造成覆盖或丢失 → 使用来源 digest、revision 和事务冲突检测；无法证明基线一致时拒绝候选并明确报告。
- [Risk] 监听器重复触发或原子写入产生多次文件事件 → 对来源事件做防抖和 digest 去重，并测试一次有效提交只产生一个 revision。
- [Risk] 消费者仍缓存旧配置，导致事件已成功但行为未变 → 对启动时构造的依赖逐一登记策略；无法安全订阅的字段必须归入 restart 分类并增加行为测试。
- [Risk] Gateway 热更新工作区注册时误删手动目标或提前关闭隧道 → 沿用来源所有权、原子 batch reconcile 和 runtime lease；在这些能力具备前将变化标记为 restart required。
- [Risk] 待重启配置长时间未应用 → 诊断和 SSE 持续暴露 pending/restart 状态，并由 UI 提供明确的受控重启入口；不伪造 active 成功。
- [Risk] 迁移逻辑改变用户文件或注释 → 保留现有备份/恢复约束，优先记录 digest 和 SQLite 状态；若必须重写 JSONC，单独验证原子写入与备份。
- [Risk] 用户级 Workspace 配置 fan-out 不是跨数据库原子操作 → 用 `fanout_id` 关联每个 Workspace 的独立 CAS/result，允许明确的 `fanout_partial`，成功 Workspace 不回滚，冲突 Workspace 不静默覆盖。
- [Risk] pending 候选或诊断泄露 provider API key → 只持久化 secret reference/version/binding digest 或自包含字面量；解析后的秘密仅在 Workspace runtime 内存中存在；字面量原文仅写入 SQLite 以支持重启恢复，任何诊断、事件、日志、outbox 和 health proof 只出现其不可逆摘要；只有无法还原的旧 `literal-sha256:` 摘要才拒绝候选。
- [Risk] 连接字段变化被错误当成新目标或旧目标 → 用持久 `connection_id` 做唯一身份，迁移时一次性生成并落盘，任何重复或缺失身份都显式失败。

## Migration Plan

1. 为现有 SQLite 配置记录补齐来源 digest、active/pending revision 和兼容的默认值；旧记录按当前 active 内容建立基线，不把旧 JSONC 无条件覆盖回数据库。
2. 分别迁移 `user`、`user_local`、`workspace` 和 runtime override 层；为用户级 Workspace JSONC 建立 source generation/fan-out 记录，并规定运行中 Workspace 的独立导入和停止后 Workspace 的启动追赶。
3. 为 Gateway `workspaces` 旧元素执行一次性 `connection_id` 迁移；迁移结果原子保存并校验重复 ID，后续 host/username/key 变化只更新同一目标 generation。
4. 发布 Workspace 重载流水线和字段策略，先覆盖当前已有 watcher、`mcp`/`logger` 重启状态以及启动时缓存依赖；新 generation 必须使用 candidate ref 和 pending loader。
5. 发布 Gateway watcher、候选应用器和安全 registry reconciliation；无法安全应用的路径先进入 pending/restart，不阻塞其他 Gateway 请求。
6. 增加诊断与事件兼容字段，并通过 focused、集成和真实双进程测试验证文件编辑、API 修改、fan-out 冲突、重启失败和联邦委托。
7. 回滚时停止新 watcher/应用器，保留 SQLite 与原始 JSONC；运行时继续使用最后一个 active revision，pending 数据可由后续版本重新识别。
8. 先落地 Gateway 自身的 active/pending/restart-intent/generation 记录和 supervisor 启动契约；普通崩溃只恢复 active，只有用户确认 intent 才允许加载 Gateway pending。
9. 在所有外部副作用完成后、promotion 前执行全部 source layer、active/pending 和 registry 基线的最终 CAS；根据副作用阶段选择停止、补偿、重建或 recovery_required。
10. 为 JSONC deletion/rename 建立 presence tombstone、source journal 顺序、备份和恢复协议，并把删除后的优先级合并结果纳入候选与重启分类。
11. 将 active snapshot、pending candidate、apply claim、outbox 和 promoted generation 持久化，验证事务提交前后崩溃、active 损坏和 secret binding 失效的启动恢复边界。
12. 保持用户 `api_key` schema 契约，在候选边界把 `${ENV}` 规范化 `secret_ref`/binding，字面量按原文持久化；迁移旧 literal/env 记录并验证 resolver failure 与 secret rotation 的 fail-closed 行为，只有不可逆 `literal-sha256:` 摘要才拒绝。
13. 由单一 source owner 提供单调 source journal/high-water mark，验证 A→B→A 事件身份和停止 Workspace 的 fan-out catch-up；digest 仅承担内容去重。
14. 将 registry batch、manual CRUD、system/projection 和 active promotion 接入同一 registry revision CAS/apply journal，明确 owner/namespace 冲突与补偿恢复。
15. 只有上述持久协议和验收场景通过后，才进入代码实现；否则继续保持 proposal/spec/design/tasks 的未实施状态。
