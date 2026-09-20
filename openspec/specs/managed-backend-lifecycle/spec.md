## Purpose

定义 Gateway 对默认及新增本地托管工作区服务的统一所有权，包括命名服务进程、排空式安全重启、显式强制中断、启动状态对账，以及关闭时完整进程组清理的可观察行为。
## Requirements
### Requirement: Gateway 拥有所有本地托管工作区
Gateway SHALL（必须）拥有默认本地工作区 runtime 和每个新增的本地托管工作区 runtime；显式声明为外部管理的本地后端 SHALL（必须）保持仅可探测。

#### Scenario: 默认工作区启动
- **WHEN** Gateway 使用有效默认工作区启动
- **THEN** Gateway 启动该工作区、将其注册为托管工作区，并提供后端重启操作

#### Scenario: 外部本地后端
- **WHEN** 工作区指向由用户管理的本地后端
- **THEN** Gateway 提供健康探测，但拒绝重启其进程

### Requirement: 按服务管理生命周期
Gateway MUST（必须）使用具名 service handle 跟踪 Workspace API、Terminal Manager 和 Browser Manager；因后端配置执行重启时 SHALL（必须）只重启 Workspace API。

#### Scenario: 仅重启后端
- **WHEN** 安全重启托管工作区后端
- **THEN** 该工作区的 Terminal、Browser 进程及服务 URL 保持不变

#### Scenario: 移除工作区
- **WHEN** 删除可移除的托管工作区
- **THEN** Gateway 优雅关闭该工作区的全部具名服务

### Requirement: 安全重启排空
Workspace API 在 draining 期间 SHALL（必须）停止接受新 Job，并 SHALL（必须）报告真实 blockers。Gateway SHALL（必须）最多等待 30 秒；若排空超时且未显式 force，SHALL（必须）保持旧后端运行。

#### Scenario: 没有活动任务
- **WHEN** 请求安全重启且后端报告没有 blockers
- **THEN** Gateway 优雅停止并重启 Workspace API，在报告成功前验证其健康状态

#### Scenario: 活动任务超过超时
- **WHEN** blockers 持续 30 秒且没有请求 force
- **THEN** Gateway 报告 blockers，并保持现有后端运行

### Requirement: 显式强制重启
Gateway MUST（必须）在中断活动任务前要求显式 force 请求；Workspace API MUST（必须）在进程终止前把受影响任务持久化为 interrupted。

#### Scenario: 用户确认强制重启
- **WHEN** 展示 blockers 后用户请求 force 重启
- **THEN** Gateway 终止后端前，将活动 Job 标记为 interrupted 并记录重启原因

### Requirement: 重启状态对账
Workspace API MUST（必须）在启动时对账已持久化的执行状态，确保没有缺少活动执行所有者的记录仍保持 running。

#### Scenario: Job 执行期间后端退出
- **WHEN** 启动时发现来自上一 runtime generation 的已持久化 running Job
- **THEN** 系统记录可观察的 interrupted 状态，而不是继续把 Job 显示为 running

### Requirement: 托管进程组清理
Gateway SHALL（必须）管理服务进程组，并 SHALL（必须）在优雅关闭超时后终止完整进程组。

#### Scenario: 子工具忽略关闭请求
- **WHEN** 托管子进程在优雅关闭期限后仍存活
- **THEN** Gateway 终止服务进程组，并报告所有清理失败

### Requirement: 需重启的配置必须通过受控生命周期应用

当 Workspace 配置候选需要重启时，Gateway SHALL（必须）使用现有 Workspace backend 的排空、阻塞项报告和重启对账流程应用 pending revision。系统 MUST（必须）避免直接杀死活动 Job；排空超时且未显式 force 时 MUST（必须）保留旧 backend 和旧 active revision。

#### Scenario: 无活动阻塞项
- **WHEN** 用户确认应用一个需重启的 Workspace 配置且 backend 没有活动阻塞项
- **THEN** Gateway 优雅重启 Workspace backend，启动健康确认后 pending revision 成为 active revision

#### Scenario: 有活动 Job
- **WHEN** Workspace backend 仍有活动 Job
- **THEN** 系统展示真实 blockers 并保持旧 backend 运行，除非用户显式确认 force

#### Scenario: 重启失败
- **WHEN** 新 backend 无法健康启动或无法加载 pending revision
- **THEN** Gateway 报告失败，保留可恢复的 pending 状态，不把旧配置或新配置错误标记为已应用

### Requirement: 配置提交必须显式处理不可事务化副作用

配置 SQLite 事务 SHALL（必须）只承诺持久化状态原子性，不得声称它能回滚 MCP 会话、logger、进程、SSH 隧道或其他外部副作用。配置应用 MUST（必须）先持久化 pending 并进入 `applying`，再执行可确认的 prepare/apply/commit 阶段；active promotion 只能发生在全部必要副作用确认成功后。

当 apply 失败时，系统 MUST（必须）优先执行补偿并恢复旧 runtime、旧 active revision 和旧连接；恢复成功则保留 pending 并报告 `restart_failed`/`pending_restart`。如果旧 runtime 已无法恢复，系统 MUST（必须）进入 `recovery_required`，报告受影响资源和恢复动作，禁止把服务或配置标为成功。每次恢复 retry MUST（必须）复用原 candidate_id 和调用方 `idempotency_key`，只创建新的 attempt_id/apply_id，不重复分配 commit_revision 或重复发布同一结果事件。

#### Scenario: 新进程启动失败且旧进程仍可用
- **WHEN** 应用需重启配置时新 Workspace backend 未通过健康检查，但旧 backend 尚未被终止
- **THEN** Gateway 放弃新 generation，继续使用旧进程和旧 active revision，pending 候选保持可重试

#### Scenario: 旧进程已停止且无法恢复
- **WHEN** 重启过程中旧进程已退出、新进程也无法启动或 SSH 隧道无法恢复
- **THEN** Gateway 进入 `recovery_required`，明确报告停机资源、旧 active revision 和 pending revision，不返回重启成功

#### Scenario: 热更新副作用补偿失败
- **WHEN** MCP/logger/运行时消费者的 apply 已产生副作用但补偿无法恢复旧状态
- **THEN** 系统进入 `recovery_required`，冻结该配置域的进一步自动提交，并保留逐资源的 apply/rollback 结果

#### Scenario: pending 候选没有真实加载证明
- **WHEN** 新 Workspace backend 返回 HTTP healthy，但未返回或返回不匹配 candidate_id、loaded revision、effective digest 的 proof
- **THEN** lifecycle controller 不得 promotion，保留旧 generation/active 和 pending，并返回 config_proof_missing 或 config_proof_mismatch

### Requirement: Gateway 自身重启必须保留旧 generation 直到新 proof 成功

Gateway 的 `restart_gateway` SHALL（必须）由稳定 supervisor/launcher 管理。supervisor 在新 Gateway generation 通过匹配的 candidate proof、全部 runtime consumer 健康检查和最终 source/registry CAS 前，MUST（必须）保留旧进程句柄、旧监听资源或能够从 active snapshot 恢复旧 generation 的启动材料；不得先杀死旧 Gateway 再尝试启动新 Gateway。需要端口交接时必须使用受控 handoff，并让 fencing token 使旧 generation 的迟到写入失效。

#### Scenario: 新 Gateway proof 成功
- **WHEN** 用户确认的 Gateway pending candidate 启动新 generation，并返回完整匹配的 proof
- **THEN** supervisor 完成最终 CAS 后再切换监听和 active generation，旧 generation 只在排空完成后退出

#### Scenario: 新 Gateway 启动失败且旧进程仍在
- **WHEN** 新 Gateway 启动失败、超时或 proof 不匹配，而旧 Gateway 仍可服务
- **THEN** supervisor 放弃新 generation，继续使用旧 active，保留 pending 并报告 restart_failed

#### Scenario: 新旧 Gateway 均不可用
- **WHEN** 旧 Gateway 已退出，新 Gateway 也无法健康启动
- **THEN** supervisor 按 active snapshot 尝试恢复旧 generation；恢复失败时进入 recovery_required，保留可定位的 runtime、generation、revision 和 fencing 证据

### Requirement: recovery_required 必须具有受控恢复出口

`recovery_required` SHALL（必须）允许显式或受信任的自动 `retry`、`resolve` 和 `discard` 操作，并且每个操作 MUST（必须）使用新的 apply_id/attempt_id 和当前 fencing token。恢复期间配置域可以阻止新的自动候选，但 MUST（必须）继续提供状态读取和恢复操作；不得永久冻结且没有出口。`resolve` 只能提交 Workspace-owned runtime proof，Gateway/人工调用不得直接修改 Workspace active/pending SQLite。

- `retry` 重新执行未完成的 prepare/apply 或旧 runtime 恢复，成功后进入 `active` 或 `pending_restart`，失败继续保持 `recovery_required` 并递增 attempt_id。
- `resolve` 只能在健康确认返回与 pending/active revision、candidate_id、digest 匹配的 generation 证明后进入 `active`；不能用人工布尔确认伪造配置已生效。
- `discard` 只能在旧 active runtime 或明确的安全基线已确认后清除 pending 并进入 `discarded`；否则保持 `recovery_required` 并返回 discard_blocked。

外部副作用的补偿 MUST（必须）通过同一 `apply_id` 的 apply journal 记录。补偿记录只允许包含资源、动作、`status=succeeded|failed` 和错误/摘要字段，不得包含候选 payload、token 或秘密；SQLite 事务回滚不得被视为外部资源已回滚。补偿成功时 journal 进入 `compensated`，补偿失败时保持 `recovery_required`，后续 discard/promotion 必须再次通过 journal 状态 CAS。

#### Scenario: 自动重试恢复旧 runtime
- **WHEN** recovery_required 的旧 generation 重新可用且自动 retry 获得 CAS claim
- **THEN** 系统确认旧 active revision 后恢复服务，pending 保留为可重试状态，不接受未证明的候选配置

#### Scenario: 人工 resolve 新 generation
- **WHEN** 管理操作提交新 generation 的健康证明，且 candidate_id、revision、digest 与 pending 完全匹配
- **THEN** 系统通过 CAS promotion active，写入恢复完成事件并解除配置域冻结

#### Scenario: 不安全地 discard
- **WHEN** 用户请求 discard 但旧 active runtime 和安全基线均无法确认
- **THEN** 系统拒绝 discard，保持 recovery_required，报告 discard_blocked 和所需恢复证据

