## 1. 配置状态模型与来源基线

- [x] 1.1 为 Gateway 与 Workspace 配置状态增加 `none`、`candidate_validated`、`pending_restart`、`applying`、`active`、`rejected`、`discarded`、`conflict`、`recovery_required` 状态及合法转移校验。
- [x] 1.2 增加 `candidate_id`、`attempt_id`、`apply_id`、完整候选、来源层 key/path、base active revision、各层 base revision/digest、candidate/effective digest、应用 generation、最近错误和持久化位置字段，并定义旧 SQLite 记录的兼容默认值。
- [x] 1.3 明确并实现 `commit_revision`、active/pending revision、effective digest、逐层 JSONC digest 和独立事件 cursor 的生成规则，失败尝试允许 `commit_revision=null`，确保内容 SHA 不承担序号语义。
- [x] 1.4 扩展 GatewayStateStore 与 WorkspaceStateStore 的配置记录读写，保证候选状态、来源 digest、apply claim 和 outbox 事件在同一事务内提交。
- [x] 1.5 为 JSONC 文件层实现稳定快照、前后 digest 检查和 CAS/TOCTOU 检查，区分首次迁移、外部文件编辑、API/UI 修改和并发冲突；诊断读取路径必须只读。
- [x] 1.6 为配置来源、SQLite 记录和 pending 状态补充单元测试，覆盖激活、显式丢弃、不重叠合并、重叠冲突、重启恢复和重复事件。
- [x] 1.7 将 `user`、`user_local`、`workspace` 和 runtime override 建模为独立 layer key，分别保存 payload、layer revision/digest、来源路径和 CAS 基线；为用户级 Workspace source 增加 source generation、fanout_id 和逐 Workspace 导入结果。
- [x] 1.8 为 pending candidate 增加脱敏规范化 payload、secret_ref/version/binding digest 和持久化位置；实现 secret resolver 边界，禁止 SQLite、outbox、诊断、日志和健康 proof 保存解析后的秘密。
- [x] 1.9 定义调用方 `idempotency_key` 与 candidate/attempt/apply 的关系及唯一约束；恢复 retry 复用 candidate/key，只创建新的 attempt/apply，不重复 commit 或同结果事件。
- [x] 1.10 为每个 JSONC layer 增加 `present`/`absent` 语义和 tombstone，定义删除、rename、临时文件、恢复文件、备份与优先级合并规则；删除不得被旧 SQLite payload 遮蔽。
- [x] 1.11 持久化精确的 active snapshot、pending candidate、source baseline 和 apply claim 结构，补齐 promotion 前后崩溃恢复、outbox 重放和 active 损坏处理。
- [x] 1.12 实现公开 `api_key` 到内部 `secret_ref`/binding 的规范化与迁移，覆盖 literal key、`${ENV}`、旧 SQLite、resolver 失败和 secret rotation，确保所有持久输出脱敏。
- [x] 1.13 由单一 source owner 实现 append-only source journal、单调 `source_generation`、source high-water mark 和 fan-out 追赶，digest 仅用于内容去重。

## 2. 统一候选重载流水线

- [x] 2.1 将 Workspace 现有 watcher、手动 reload 和配置 API 接入统一的候选生成、完整校验、diff、策略分类和原子提交入口。
- [x] 2.2 实现与 VS Code 类似的短暂防抖和 digest 去重，确保连续文件事件只形成一次候选提交和一次有效变化事件。
- [x] 2.3 实现 active snapshot、pending snapshot 和 applying 状态的分离；候选失败或需重启时不得部分提交，并保留可诊断错误。
- [x] 2.4 为 watcher、API/UI、手动重启和启动恢复实现 SQLite CAS claim、lease/deadline、apply_id 和 generation fencing；补充超时、崩溃、遗留 applying 的恢复规则。
- [x] 2.5 为 Workspace 新 generation 定义不透明 candidate_ref 启动契约与 Workspace-owned pending loader，回显 loaded_source、candidate_id、loaded revision、effective/candidate digest、secret binding digest 和 generation id；缺 ref 只读 active，有 ref 只读匹配 pending，禁止静默回退；只允许匹配证明 promotion active。
- [x] 2.6 实现 JSON Pointer 精确路径、对象通配符、数组 identity key 和最长匹配策略；未登记但 schema 合法的字段按配置域默认到 `restart_workspace`/`restart_gateway`，未知字段仍由 schema 拒绝。
- [x] 2.7 将启动时缓存配置的消费者逐项登记到策略表，至少覆盖 MCP、logger、Job timeout、Terminal/Browser URL 和 Gateway connection URL，修复“状态已更新但运行时仍用旧值”的误报。
- [x] 2.8 建立策略表到 schema 注释、诊断和测试的生成/一致性校验，统一 policy、result、activation_scope 和 path 状态命名。
- [x] 2.9 为 Workspace 热重载增加模型/Agent、默认 Agent、UI、按请求运行参数、需重启参数的 focused tests，验证后续 Job、后续会话和当前 Job 的边界。
- [x] 2.10 在每次外部 apply 完成后、active promotion 之前重新 CAS 全部 source layer 的 presence/revision/digest、source generation、active/pending revision 和适用的 registry revision；冲突时执行重建或补偿。
- [x] 2.11 为 apply journal 记录 source、active、pending、registry 的完整基线和逐资源副作用，保证最终 CAS 失败时不产生 active 成功事件并可恢复。

## 3. Gateway 配置热重载

- [x] 3.1 在 Gateway lifespan 中启动和停止配置 watcher，复用完整 Gateway 配置加载、schema 校验、来源诊断和候选应用入口。
- [x] 3.2 为 Gateway 建立配置生效策略，覆盖 UI、目录/生成器调度、健康检查、工作区注册、SSH 隧道和不可热替换运行时依赖。
- [x] 3.3 让可热更新的 Gateway 消费者通过配置订阅或下一次操作读取新快照，避免只更新 `app.state.gateway_config` 而留下启动时缓存。
- [x] 3.4 为 registry 目标补齐稳定 target id、`config`/`manual`/`system`/`remote_projection` owner、generation、runtime lease 和活动请求/流引用。
- [x] 3.5 分别实现目录/Generator scheduler、health controller、registry batch、SSH tunnel/proxy、Workspace process 和 remote projection 的 prepare/apply/health/promotion/rollback 协议。
- [x] 3.6 将托管工作区注册变化接入来源所有权、原子 batch reconcile 和 runtime lease；在无法安全对账时保留旧 active 并生成 restart required pending 状态。
- [x] 3.7 为 SSH 隧道、代理连接和 SSE/WebSocket 使用者增加排空前置检查，未排空时不得提前关闭仍在使用的资源。
- [x] 3.8 增加 Gateway 配置修改、注册增删改、owner 保护、重复事件、重载失败和 Gateway 重启后 pending 提升为 active 的测试。
- [x] 3.9 为 `gateway.jsonc` 的 `workspaces` 元素增加持久 `connection_id`；实现旧数组一次性 ID 迁移、重复/歧义拒绝，以及 host/username/key 变化时同 ID generation 更新、健康切换和活动 lease 排空。
- [x] 3.10 实现 Gateway 自身 pending 协议：持久 restart intent、Gateway candidate_ref、Gateway-owned pending loader、runtime generation/health proof、fencing token，以及 supervisor 保留旧 generation 和失败回退。
- [x] 3.11 为 registry_meta revision、config batch、manual CRUD、system 更新、remote projection 和 active promotion 实现统一 revision CAS、apply journal、命名空间唯一性与并发恢复；禁止整表删除重插入。
- [x] 3.12 补充 Gateway runtime matrix 的 Gateway process 项，区分普通崩溃恢复与用户确认 pending 应用，并记录监听 handoff、旧 generation 排空和 recovery_required 边界。

## 4. 变化事件与诊断 API

- [x] 4.1 定义独立于 Job 分区事件总线的 Gateway/Workspace 配置事件模型，包含唯一 `event_id`、域内单调 cursor、candidate/attempt/apply id、调用方 `idempotency_key`、可空 commit/active/pending revision、source、result、activation scope、changed/applied/deferred paths 和 error。
- [x] 4.2 为配置状态和事件建立同一 SQLite 事务写入的 append-only outbox，增加 after-cursor replay、cursor 过期的 `snapshot_required`、生产者幂等键、relay 重试和消费者 event_id 去重。
- [x] 4.3 明确统一结果映射：`applied`、`restart_required`、`restart_failed`、`apply_failed`、`rejected`、`conflict`、`discarded`、`recovery_required`、`unchanged`；`deferred` 只表示路径/activation scope，不作为 result。
- [x] 4.4 明确事件产生时机：active promotion 或 pending/rejected/conflict/discarded/recovery_required/restart_failed/apply_failed 状态事务提交后进入 outbox，候选解析中不得发布成功事件。
- [x] 4.5 将配置事件接入独立 SSE/订阅接口，不能把 Job 事件总线当作配置事件流；确保重复 watcher 不重复产生 commit/event。
- [x] 4.6 扩展 Workspace 配置状态接口，返回 active/pending revision、各层 digest、应用策略、candidate/attempt/apply 状态、applied/deferred paths 和最近失败信息。
- [x] 4.7 增加 Gateway 对应配置重载状态和事件接口，并明确区分诊断重新读取与实际候选提交，诊断路径不得写库。
- [x] 4.8 为两个配置域增加 API 契约测试，验证 applied/deferred 的整体原子语义：整体 pending 时 `applied_paths=[]` 且 `deferred_paths=changed_paths`。
- [x] 4.9 为 fan-out、秘密脱敏和恢复 retry 增加诊断/API 契约测试：逐 Workspace 展示 revision/result，敏感字段只显示 reference/digest，同 candidate retry 不重复 commit/event。

## 5. 受控重启与联邦委托

- [x] 5.1 将 Workspace pending restart 接入现有 Gateway drain、blockers、force 和启动对账流程，确保超时不误杀且新 backend 健康后才提交 active。
- [x] 5.2 实现 prepare/apply/commit 与补偿边界，明确 SQLite 事务不回滚 MCP、logger、进程或 SSH 隧道副作用。
- [x] 5.3 重启失败时优先恢复旧 generation、旧 active 和旧连接；旧 runtime 无法恢复时进入 `recovery_required`，记录资源、revision 和恢复动作。
- [x] 5.4 为 `recovery_required` 实现 CAS claim 下的 retry、带匹配健康证明的 resolve、需要安全基线才能执行的 discard，并定义失败后重新进入 recovery_required 的规则。
- [x] 5.5 为重启失败增加 pending 保留、旧 active/新 pending 状态校验、旧进程恢复和启动遗留 applying 处理测试，避免错误报告为配置已生效。
- [x] 5.6 明确本地 Gateway 与远程 Gateway 的配置所有权，禁止本地直接修改远程工作区文件或绕过远程 Gateway 生命周期。
- [x] 5.7 增加远程 Gateway 配置需重启、远程离线、事件 cursor 断档、重连成功和投影更新的联邦集成测试。
- [x] 5.8 固化 Gateway 只传递 candidate_ref、接收 Workspace proof/result 的接口边界，禁止 Gateway 读取/修改 Workspace SQLite；为本地与远程委托分别验证该所有权约束。
- [x] 5.9 为 Gateway 自身 process 实现稳定 supervisor/launcher 的旧新 generation 管理、受控 listener handoff、失败回退、active snapshot 恢复和 fencing。
- [x] 5.10 为 apply 期间 source 二次修改实现最终 CAS 失败路径、无副作用停止、已有副作用补偿和 recovery_required 证据。

## 6. 配置契约、迁移与回归验证

- [x] 6.1 更新 Gateway/Workspace schema 或配置策略文档，记录每个受支持配置段的即时、后续对象或重启生效范围；为 Gateway `workspaces` 元素声明 `connection_id`，并记录旧配置版本的可恢复迁移。
- [x] 6.2 更新配置迁移与诊断命令，使其能报告 JSONC/SQLite 来源、各层 digest、commit/active/pending revision、candidate/attempt/apply 状态和冲突；诊断不得静默迁移、备份或覆盖用户配置。
- [x] 6.3 运行配置 schema、配置服务、Gateway、生命周期和联邦的 focused tests，并检查正式测试工作区均写入对应 `out/tests/` 路径。
- [x] 6.4 将验收矩阵固化为测试用例，至少覆盖来源矩阵中的 JSONC/API/UI/session/registry/remote projection 边界、文件/API 冲突、并发 TOCTOU、重复 watcher、混合修改整体 pending、重启失败后的旧进程恢复或 `recovery_required`、registry owner 保护与原子对账、远程 Gateway 委托/断档、诊断只读、pending 候选真实加载 proof、用户级 Workspace fan-out/逐 Workspace 冲突、`connection_id` 在 host/username/key 变化时的对账。
- [x] 6.5 通过真实双进程验证 Workspace 与 Gateway 的文件编辑、API/UI 修改、独立配置事件 replay、后续 Job/会话生效、需重启行为和失败恢复；真实链路使用 API 配置入口和本地 OpenAI 协议替身，Web 专用配置编辑入口、真实外部 Provider 以及 Windows/容器平台边界仍未验证。
- [x] 6.6 执行 `openspec validate add-config-hot-reload-parity --type change --strict`，确认 proposal、delta specs、design 与 tasks 一致后再进入实现阶段。
- [x] 6.7 增加删除 JSONC、absent tombstone、rename/临时文件/恢复文件和被高层遮蔽的 unchanged 验收测试。
- [x] 6.8 增加 apply 期间 source 二次修改的最终全量 CAS、无副作用停止、补偿失败和 recovery_required 验收测试。
- [x] 6.9 增加 source journal 的 A→B→A、重复 A→A、停止 Workspace high-water catch-up 和 fanout_partial 验收测试。
- [x] 6.10 增加旧 SQLite literal/env secret 迁移、resolver 失败、secret rotation 和 proof 脱敏验收测试。
- [x] 6.11 增加 connection_id 迁移的旧 schema 校验、崩溃恢复、幂等重试、注释保留、partial source layer 和 v1 rollback 验收测试。
- [x] 6.12 增加 Gateway 自身 pending 重启、普通崩溃恢复、candidate_ref/proof mismatch、真实监听 handoff 和旧 generation 回退验收测试；已覆盖真实子进程 pending load、loader 早期失败、stale generation 拒绝、runtime 初始化后 stale generation/fencing 失败回报拒绝、稳定 public listener 的真实交接和失败时旧 generation 保持服务。
- [x] 6.13 增加 manual CRUD 与 config batch/promotion 并发、registry revision CAS、owner/namespace 唯一性和 apply journal 恢复验收测试。
