## MODIFIED Requirements

### Requirement: Gateway 拥有所有本地托管工作区

Gateway SHALL（必须）拥有默认本地工作区 runtime 和每个新增的本地托管工作区 runtime；显式声明为外部管理的本地后端 SHALL（必须）保持仅可探测。一个本地托管 Backend MAY 挂载多个工作区，但其进程句柄、generation 和完整 mount 清单 MUST 由一个 Gateway managed Backend owner 唯一持有。工作区成员 MUST 保留自己的身份、业务数据、路由、运行意图与辅助服务，只引用该 owner，MUST NOT 各自持有或关闭同一 Backend 句柄。

共享关系 MUST 来自 Gateway 控制面显式登记的一份 owner 成员集合/启动合同；MUST NOT 根据相同 URL、端口、根目录相似性或所有 `managed && desired_running` target 猜测共享。清单 MUST 绑定每个 Gateway target ID、后端 workspace UUID 与规范化根；后端的已挂载工作区注册表仍唯一拥有该 generation 的实际寻址事实。Gateway 只管理控制面合同，MUST NOT 直接读取工作区业务数据。

#### Scenario: 默认工作区启动

- **WHEN** Gateway 使用有效默认工作区启动
- **THEN** Gateway 启动该工作区、将其注册为托管工作区，并提供后端重启操作

#### Scenario: 外部本地后端

- **WHEN** 工作区指向由用户管理的本地后端
- **THEN** Gateway 提供健康探测，但拒绝重启其进程

#### Scenario: 一个托管后端挂载多个工作区

- **WHEN** 一个托管后端进程按本 change 的注册表挂载多个工作区
- **THEN** 每个工作区的业务身份独立，同一 Backend 的进程句柄只属于一个 owner；成员关闭不得提前关闭仍服务 sibling 的进程

#### Scenario: 普通新增工作区

- **WHEN** 用户按现有入口新增未显式指定共享关系的本地托管工作区
- **THEN** Gateway MUST 保持原独立 Backend 启动行为，创建 singleton owner，MUST NOT 自动并入已有共享组或无故改成未运行

#### Scenario: 相同 URL 不证明共享所有权

- **WHEN** 外部管理的本地后端与托管 target 使用相同 URL
- **THEN** 外部 target MUST 仍只可探测/转发，不成为 managed owner 的成员，Gateway MUST NOT 创建、重启或关闭其进程

### Requirement: 按服务管理生命周期

Gateway MUST（必须）使用具名 service handle 跟踪 Workspace API、Terminal Manager 和 Browser Manager；因后端配置执行重启时 SHALL（必须）只重启 Workspace API。共享 Backend 的 Workspace API handle MUST 只由其唯一 owner 管理；Terminal/Browser handle MUST 继续按工作区管理。`remove`、runtime 替换、shutdown、preserve/adopt 和 startup recovery MUST 按 Backend owner 去重，MUST NOT 将同一 handle 复制到各成员 runtime 后逐个关闭或接管。

#### Scenario: 仅重启后端

- **WHEN** 安全重启托管工作区后端
- **THEN** 该工作区的 Terminal、Browser 进程及服务 URL 保持不变

#### Scenario: 移除工作区

- **WHEN** 删除可移除的托管工作区
- **THEN** Gateway 优雅关闭该工作区的全部具名服务；若其属于共享 Backend，成员移除 MUST 遵守完整组排空与新 mount 集合切换，保留成员的辅助服务不得被关闭

#### Scenario: Gateway 关闭或接管共享进程

- **WHEN** Gateway shutdown 或 generation handoff 涉及同一共享 Backend 的多个成员
- **THEN** Gateway MUST 排空全组路由引用，并且只关闭或转交该 Backend handle 一次；每个成员的辅助服务仍按原策略处理

### Requirement: 安全重启排空

Workspace API 在 draining 期间 SHALL（必须）停止接受新 Job，并 SHALL（必须）报告真实 blockers。Gateway SHALL（必须）最多等待 30 秒；若排空超时且未显式 force，SHALL（必须）保持旧后端运行。共享 Backend 的排空 MUST 汇总完整成员集合中的 Job、tool、background task 和 Gateway proxy request/stream 引用。每个 blocker MUST 带其 Gateway `workspace_id`，保留既有 kind、resource/session/status/detail 语义；超时 MUST 撤销 drain，保留全体旧 generation、mount 集合、运行意图与配置 active 状态。

#### Scenario: 没有活动任务

- **WHEN** 请求安全重启且后端报告没有 blockers
- **THEN** Gateway 优雅停止并重启 Workspace API，在报告成功前验证其健康状态；共享 Backend MUST 验证完整 mount 集合的 UUID、根与健康

#### Scenario: 活动任务超过超时

- **WHEN** blockers 持续 30 秒且没有请求 force
- **THEN** Gateway 报告 blockers，并保持现有后端运行

#### Scenario: sibling 仍有 blocker

- **WHEN** 发起工作区已排空但同一 Backend 的其它成员仍有活动执行或代理流
- **THEN** Gateway MUST 返回带成员归属的真实 blockers 与完整影响集合，MUST NOT 只按发起工作区宣布已排空或关闭进程

### Requirement: 显式强制重启

Gateway MUST（必须）在中断活动任务前要求显式 force 请求；Workspace API MUST（必须）在进程终止前把受影响任务持久化为 interrupted。共享 Backend 的 force MUST 先通过本 capability 的完整组 generation/成员前置校验，再持久化所有成员的受影响执行；MUST NOT 将一个 target 的 force 隐式扩大为 sibling force。

#### Scenario: 用户确认强制重启

- **WHEN** 展示 blockers 后用户请求 force 重启
- **THEN** Gateway 终止后端前，将活动 Job 标记为 interrupted 并记录重启原因

#### Scenario: 强制重启共享 Backend

- **WHEN** 调用方显式提交匹配的完整影响集合和 generation，并确认 force
- **THEN** 全组受影响执行 MUST 先持久化为 interrupted，再关闭唯一 Backend handle；确认 UI MUST 展示所有会受中断的工作区

### Requirement: 重启状态对账

Workspace API MUST（必须）在启动时对账已持久化的执行状态，确保没有缺少活动执行所有者的记录仍保持 running。共享 Backend 故障 MUST 使当前 generation 全部成员路由失效，并逐成员公开 offline/connection error；恢复 MUST 由 Backend owner 只执行一次，按控制面权威启动合同恢复完整 mount 集合。身份、根或成员集合不符 MUST 拒绝 adopt，MUST NOT 逐 target 重复启动、部分恢复或把未验证的 sibling 显示在线。

#### Scenario: Job 执行期间后端退出

- **WHEN** 启动时发现来自上一 runtime generation 的已持久化 running Job
- **THEN** 系统记录可观察的 interrupted 状态，而不是继续把 Job 显示为 running

#### Scenario: 共享进程退出与启动恢复

- **WHEN** 共享 Backend 退出或 Gateway 启动时需要恢复该 owner
- **THEN** Gateway MUST 统一失效其全部成员路由，按精确完整启动合同仅恢复/接管一次，并在全部 mount 验证成功后公布在线

### Requirement: 托管进程组清理

Gateway SHALL（必须）管理服务进程组，并 SHALL（必须）在优雅关闭超时后终止完整进程组。共享 Backend 的清理 MUST 按唯一 owner 执行一次；移除最后成员时 MUST 关闭该 Backend 并释放 owner，成员辅助服务继续按其所属工作区清理。所有失败 MUST 可观察，MUST NOT 以其它成员仍持有的重复句柄伪报关闭成功。

#### Scenario: 子工具忽略关闭请求

- **WHEN** 托管子进程在优雅关闭期限后仍存活
- **THEN** Gateway 终止服务进程组，并报告所有清理失败

#### Scenario: 删除最后一个共享成员

- **WHEN** 全部成员经受控排空与清理退出
- **THEN** 唯一 owner MUST 关闭 Backend handle 一次并释放控制面登记，不留下可被其它 target 再次接管的陈旧句柄

### Requirement: 需重启的配置必须通过受控生命周期应用

当 Workspace 配置候选需要重启时，Gateway SHALL（必须）使用现有 Workspace backend 的排空、阻塞项报告和重启对账流程应用 pending revision。系统 MUST（必须）避免直接杀死活动 Job；排空超时且未显式 force 时 MUST（必须）保留旧 backend 和旧 active revision。共享 Backend MUST 遵守完整组前置校验与排空；candidate promotion proof MUST 只提交发起 target 的匹配 pending candidate，其它 mount MUST 验证各自 UUID、根与健康，MUST NOT 被顺带 promotion。候选失败 MUST 恢复完整旧组与旧 mount 集合；恢复失败 MUST 按原配置副作用合同进入 `recovery_required`，MUST NOT 仅恢复发起成员并伪报全组成功。

#### Scenario: 无活动阻塞项

- **WHEN** 用户确认应用一个需重启的 Workspace 配置且 backend 没有活动阻塞项
- **THEN** Gateway 优雅重启 Workspace backend，启动健康确认后 pending revision 成为 active revision

#### Scenario: 有活动 Job

- **WHEN** Workspace backend 仍有活动 Job
- **THEN** 系统展示真实 blockers 并保持旧 backend 运行，除非用户显式确认 force

#### Scenario: 重启失败

- **WHEN** 新 backend 无法健康启动或无法加载 pending revision
- **THEN** Gateway 报告失败，保留可恢复的 pending 状态，不把旧配置或新配置错误标记为已应用

共享配置候选的proof缺失/不匹配、sibling身份/健康失效和副作用补偿失败 MUST 以owner generation与完整成员集合为边界。缺少匹配的Workspace-owned candidate proof或完整mount验证时 MUST NOT promotion；失败按原配置合同恢复完整旧启动合同/active，不局部公布在线。配置域的retry/resolve/discard继续由原owner/CAS/journal处理，且必须同时满足完整组生命周期前置与mount验证；恢复操作不能绕过成员确认或直接改工作区SQLite。

#### Scenario: 共享 Backend 的单成员配置候选

- **WHEN** 一个成员的 pending candidate 发起完整组重启
- **THEN** Gateway MUST 验证所有 mount，只凭该成员的匹配 candidate proof promotion 该成员；失败时按原合同恢复完整旧组或公开全组 recovery_required

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

共享Backend的唯一owner、handle与完整已公布启动清单 MUST 保留到新Gateway的匹配candidate proof、全部runtime consumer验证及最终source/registry CAS成功；全组成员必须一同接管，不能提前detach/close或逐target重复adopt。proof失败时旧Gateway继续服务完整旧组；新旧Gateway均不可用时按原active snapshot和完整owner启动合同恢复，失败公开全组offline/recovery_required，不伪报部分接管成功。

#### Scenario: Gateway候选proof失败涉及共享owner

- **WHEN** 新Gateway候选启动失败或proof/CAS未通过，且旧Gateway仍可服务共享Backend
- **THEN** 旧owner/handle/完整成员路由 MUST 继续服务，不因候选逐target关闭或转交共享Backend

#### Scenario: Gateway新generation完整接管

- **WHEN** 新Gateway通过匹配proof、完整runtime consumer验证及最终CAS
- **THEN** 共享Backend MUST 按唯一owner一次接管完整成员，旧Gateway只在排空后释放，不重复adopt或丢失sibling

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

共享Backend的配置域recovery_required仍由原配置owner管理，MUST 绑定该apply的owner generation、完整旧/目标mount集合及发起成员candidate。retry恢复原完整组，resolve只接受发起成员匹配的Workspace-owned proof并验证全部sibling mount，discard必须确认完整旧组/安全基线而非仅发起成员；任一拟恢复成员未验证时 MUST NOT 将该候选/恢复generation的任何成员公布在线，禁止部分promotion；仍可确认的完整旧组继续按原合同服务。普通非配置组切换的offline恢复使用本capability规定的完整组start，不另建配置恢复状态机。

#### Scenario: 共享配置recovery只恢复一个成员

- **WHEN** retry/resolve/discard仅取得发起成员proof或健康，尚未确认owner完整成员集合及sibling身份/健康
- **THEN** 系统 MUST 拒绝promotion/成功discard或全组恢复结论，保原配置恢复状态并报告缺失成员，不能部分公布在线

## ADDED Requirements

### Requirement: 共享 Backend 生命周期必须显式确认完整 generation 和成员集合

singleton 的现有 start/stop/restart 入口 MUST 保持原行为与默认工作区不可 stop 的限制。共享 Backend 的普通单 target 操作 MUST 返回 `blocked`，包含准确完整的 `affected_workspace_ids` 和原因，MUST NOT 修改任一成员运行意图或中断 sibling。明确整组操作 MUST 复用现有 Gateway lifecycle 入口，并同时提交 `expected_backend_generation` 与无重复的 `expected_workspace_ids`；后者使用 Gateway 控制面 target ID，与路由 `{workspace_id}` 同一命名空间，MUST 精确等于 owner 当前完整成员集合。缺字段、陈旧 generation、漏项、多项或非成员 MUST 在任何 drain/意图写入前明确拒绝。

同一 owner 的 start、stop、restart、成员变更、配置 restart 和自动恢复 MUST 由一把 owner 级生命周期锁串行，锁内重验 generation/成员前置条件；各 target 的锁 MUST NOT 被当作共享进程的互斥保证。runtime 状态与生命周期响应 MUST 提供 `backend_generation`、完整 `affected_workspace_ids`，所有 blocker MUST 提供归属 `workspace_id`；Web 请求、状态、阻塞反馈与 force 确认 MUST 消费同一影响集合。身份与影响面字段 MUST 同步迁齐 DTO、OpenAPI、必要协议、直接/间接消费者及 fixture，不保旧共享行为兼容层。

默认工作区不可 stop 的限制 MUST 适用于完整组：只要影响集合包含 `system_default`，整组 stop MUST 在任何 drain/意图变更前拒绝，完整组 restart仍可按原合同执行。响应 `backend_generation` MUST 指向当前或最近已公布的 owner generation；停机后保留该精确generation/权威成员集合供明确整组start恢复，MUST NOT 用空值或猜测集合绕过前置校验。成员变更的 `affected_workspace_ids` MUST 覆盖旧集合与目标集合的并集，普通restart/stop则为当前完整组。

#### Scenario: 普通单 target 不能重启 sibling

- **WHEN** 一个共享组成员被按普通单 target 路径 start/stop/restart
- **THEN** Gateway MUST 返回 blocked 与全组影响集合，旧 generation、成员意图及 sibling 业务保持不变

#### Scenario: 完整组前置条件匹配

- **WHEN** 同一 owner 锁内校验请求 generation 和完整成员集合均匹配
- **THEN** Gateway MAY 执行整组生命周期，并报告完整影响集合；只匹配路径 target 不足以授权执行

#### Scenario: 陈旧请求或并发整组动作

- **WHEN** 另一个动作已切换 generation 或成员集合，或同组操作同时到达
- **THEN** Gateway MUST 串行执行并重验前置条件，拒绝陈旧请求，MUST NOT 使用旧确认中断新 generation 的执行

### Requirement: generation 内 mount 集合不可变且成员变更必须整组切换

Backend generation 启动后的 mount 集合 MUST 不可变。运行中的显式加入/退出共享组、移除共享成员或其它改变实际 mount 集合的操作 MUST 在唯一 owner 中准备目标集合，先校验当前完整 generation/成员并排空当前全组，再创建和验证目标集合的新 generation，最后公布新成员/路由。新集合未验证前 MUST NOT 将新增成员标为可用；失败 MUST 恢复完整旧集合，不能留下部分在线组或错误的运行意图。MUST NOT 预造动态 mount RPC、从磁盘重建成员关系或偷偷合并独立 singleton。

成员变更 MUST 显式提供 `desired_workspace_ids`，使用无重复的 Gateway target ID 完整目标集合，与 `expected_workspace_ids` 的当前集合严格分开。加入项 MUST 是已登记的本地managed target，不能属于另一个活动Backend owner；运行中的singleton必须先经自身受控stop，不自动合并两个活动owner。移除项 MUST 属于当前集合；默认工作区不可被移除。所有目标身份/成员/所有权条件 MUST 在drain和意图写入前校验；路径target、`desired_running`或当前集合筛选不能代替完整目标输入。新owner切换成功前，旧成员权威与运行意图 MUST 保持；目标集合只属于此次待验证操作，不建立第二份可运行成员权威。

普通成员变更或整组restart若新组失败且旧组也无法恢复，Gateway MUST 保持旧完整成员集合为唯一已公布权威，全组offline/路由失效，并保留本次目标集合、失败generation及错误作为操作诊断，MUST NOT 伪报恢复或让部分成员在线。显式完整组start MUST 作为owner恢复出口：校验最近已公布generation和完整旧集合，按原已公布启动合同重建一次并全量验证；成功公布恢复generation，失败仍全组offline。需要改目标集合时必须另发完整成员变更命令，不借retry采纳失败候选；不把配置域的recovery_required自动套给普通成员变更。

已经停机的 owner MUST 允许相同完整前置条件下的合法显式成员变更与 DELETE，无需先启动旧 Backend。操作 MUST 在同一拓扑锁与 owner 锁内确认旧进程和辅助运行时已停止或完成清理，并校验目标身份、所有权及路由引用；safe 被引用阻挡时保留旧权威，force 只沿既有引用 owner 撤销并等待释放，不向不存在的 Backend 请求业务中断。非空目标 MUST 原子公布同一 owner 的新 generation、完整目标启动合同及必要 target 删除，受影响成员仍 offline、运行意图为停止且不持有成员 runtime lease，MUST NOT 启动业务进程或伪报在线。清理、校验或提交失败 MUST 保留旧 generation、成员集合和 target 登记；已提交后的观察者错误不得回退权威状态。后续显式完整组 start MUST 使用新 generation 和完整剩余启动合同，验证全部成员后才公布在线；旧 generation 请求 MUST 被拒绝。普通 stop/start 不变更成员时仍遵循原 generation 合同。在线及停机 DELETE 的 target 删除、子节点解绑与剩余 owner 合同 MUST 在同一次权威 SQLite snapshot/CAS 提交中落地；写入 owner 范围不得把被删除的 manual target 合并保留，重开数据库后仍须确认精确删除及其它 owner/sibling 登记不变。

目标集合为空时 MUST 只完成已确认的最后成员退出与唯一close，不启动空mount进程；有默认成员的集合不得以stop或remove使默认成员退出。

`desired_workspace_ids` 未提供与显式提供空集合 MUST 有不同语义：未提供表示本次不请求改变成员集合，显式空集合表示确认全部当前成员退出。HTTP DTO、协议载体、生成绑定与解码映射 MUST 保留该 presence，MUST NOT 把缺省 repeated 字段的空值解释为退出全部成员。发起路由的工作区 MUST 属于 expected 当前集合，但 MAY 不属于 desired 目标集合；退出发起成员不能被提前拒绝。

现有 `DELETE /api/gateway/workspaces/{workspace_id}` 删除本地托管 target 时 MUST 委托同一 owner 级完整组成员退出流程，MUST NOT 经 registry CRUD 直接关闭活跃 Backend。共享组删除 MUST 使用同一 `GatewayRuntimeGroupRequest` 承载 generation 与完整当前成员确认；DELETE 的目标集合 MUST 等于当前集合去掉路由指定 target，显式 desired 不匹配时 MUST 在副作用前拒绝，MUST NOT 借此添加成员或删除其它 target 登记。force MUST 由显式请求确认，运行中先取得全组 interrupted 持久化确认；停机时沿上述引用释放合同执行。safe 被真实 blockers 阻挡时 MUST 返回 HTTP 409，detail 携带完整 typed lifecycle 结果与准确影响集合，保持旧成员、owner、运行意图与 target 登记；成功删除仍返回完整工作区列表。Web、直接托管与联邦调用方 MUST 消费同一结果，失败不得伪造删除成功，页面观察流按既定租约恢复。外部管理与远程 target 保留原控制面删除归属，不被当成本地 managed 进程关闭；存在 SSH 转发时 MUST 先成功清理转发再删除 target，清理失败 MUST 保留 target 登记并报告真实错误。

#### Scenario: 停机共享组删除非末成员后恢复

- **WHEN** 已停机的共享组在完整 generation/当前成员确认下删除一个可移除成员，目标集合仍非空
- **THEN** Gateway MUST 在同一 owner 原子公布剩余集合的新 generation 和完整启动输入，删除指定 target，剩余成员保持 offline 且不启动 Backend；旧 generation 请求被拒绝，后续显式 start 验证完整剩余组后才公布在线

#### Scenario: 停机退出的清理或发布失败

- **WHEN** 停机成员退出的进程/辅助清理、身份校验或原子发布失败
- **THEN** 系统 MUST 保留旧 generation、完整成员和 target 登记并报告原因，不重新启动旧组；提交后的观察者错误不得回退已提交权威

#### Scenario: 远程 target 转发清理失败

- **WHEN** DELETE 远程 target 的 SSH 转发清理失败
- **THEN** Gateway MUST 返回真实失败并保留 target 登记及未清理转发供重试，MUST NOT 先删除 target 后留下失去归属的转发

#### Scenario: 缺省目标集合不能退出全部成员

- **WHEN** 请求没有提供 desired 集合，或协议解码只得到缺省 repeated 空值
- **THEN** 系统 MUST 保留原成员集合；只有有 presence 的显式空集合及匹配完整前置条件才能进入全部成员退出流程

#### Scenario: 发起成员可以退出完整组

- **WHEN** 发起 target 属于完整 expected 集合，而 desired 合法集合不再包含该 target
- **THEN** 系统 MUST 按同一 owner 完成排空与成员切换，MUST NOT 仅因发起成员退出而拒绝；desired 为空时只关闭和释放 owner，不启动空 mount Backend

#### Scenario: 删除最后成员被活动任务阻挡

- **WHEN** safe DELETE 删除最后一个可移除托管成员，而 Backend 仍有 Job、tool、background 或代理引用 blocker
- **THEN** Gateway MUST 返回 HTTP 409 的完整 typed blocker 结果，撤销 drain 并保持原 owner、target、运行意图与进程，不经 registry.remove 绕过阻塞

#### Scenario: 强制删除最后成员先持久化中断

- **WHEN** 调用方确认匹配完整 generation 与成员集合，并显式 force 删除最后成员
- **THEN** Backend MUST 先持久化受影响执行的 interrupted 状态，再唯一关闭 handle、清理所属辅助服务并释放 owner/target，且不创建空 mount 进程

#### Scenario: 显式加入共享组

- **WHEN** 一个新或已登记 managed target 显式请求加入共享组
- **THEN** 它 MUST 经完整组排空和 generation 重建后才成为可用成员；普通新增 singleton 行为不受影响

#### Scenario: 移除一个共享成员

- **WHEN** 调用方明确确认当前完整组并移除其中一个可移除成员
- **THEN** Gateway MUST 以剩余 mount 集合完成新 generation 切换，仅清理退出成员的辅助服务，保留成员业务身份和数据不变
