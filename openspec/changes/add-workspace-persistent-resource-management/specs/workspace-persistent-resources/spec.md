## Purpose

让用户在工作区内发现和管理 Browser、Terminal 等跨 Turn 资源，并明确资源何时随 Thread 回收、何时脱离 Thread 生命周期长期保留。

## ADDED Requirements

### Requirement: 领域 owner 是跨 Turn 资源的唯一权威

每项跨 Turn 资源 MUST 具有稳定的 `resource_id`，并由对应领域 owner 权威维护资源状态、保留范围、不可变来源 `(session_id, thread_id)`、当前 Thread 关联、operation lease 和外部副作用。保留范围 MUST 是 `thread` 或 `workspace`。Browser、Terminal 等 Agent 资源创建工具 MUST 暴露 `retention_scope` 参数且默认值 MUST 为 `thread`；省略该参数不得隐式创建持久资源。用户或工具参数明确选择 `workspace` 时，资源才可在创建时持久化；用户或明确的工具操作也可将已有 `thread` 资源提升为 `workspace`。Thread 范围资源只关联其来源 Thread；即使解除关联，owner 仍 MUST 保留来源身份以便 Thread unload 或 Session 删除时清理。`workspace` 资源可以关联同工作区内零个或多个 Thread。工作区持久记录 MUST 存在于 `${workspace_abs_path}/.boxteam/` 下的 owner 存储，并独立于 Session 物理目录；Session/Thread 只保存可解析的资源引用，不复制资源详情或运行句柄。通用 Registry MUST 只向 owner 查询、路由命令并投影结果，不得另存生命周期事实、推断资源是否停止或绕过 owner 修改外部资源。

本 requirement 中的 `workspace_id` 身份与「哪个工作区」的确定方式 MUST 以 `add-multi-workspace-backend-mounting` change 的**已挂载工作区注册表**与显式寻址载体为准：一个后端进程 MAY 挂载多个工作区，`${workspace_abs_path}` MUST 由显式 `workspace_id` 解析得到，MUST NOT 依赖「当前激活工作区」；来源 `(session_id, thread_id)` 所属工作区 MUST 一并作为显式身份保留，跨工作区引用 MUST 使用寻址层身份而非进程激活态。

#### Scenario: 聚合查询返回 owner 当前权威投影

- **WHEN** 用户通过工作区资源 API 请求资源列表，且资源 owner 可用
- **THEN** 每个资源的类型、保留范围、关联和生命周期状态均来自对应 owner 的当前权威记录，聚合器不生成或持久化第二个资源状态

#### Scenario: 资源 owner 不可用时列表不能伪装为空

- **WHEN** 工作区资源列表需要查询的领域 owner 不可用或无法核对状态
- **THEN** API 明确返回 owner 错误或带有明确缺失类型的不完整结果，不得把故障静默投影为资源不存在或空列表

#### Scenario: Agent 资源创建默认非持久化

- **WHEN** Agent 使用 Browser、Terminal 或其它跨 Turn 资源创建工具，且未指定 `retention_scope`
- **THEN** 资源以 `thread` 范围创建并关联精确来源 Thread；工具不得把省略参数解释为 `workspace`

#### Scenario: Agent 显式选择持久范围创建资源

- **WHEN** 用户明确要求持久化，或 Agent 工具调用显式指定 `retention_scope=workspace`
- **THEN** owner 创建 `workspace` 范围资源并关联来源 Thread；后续 Thread unload 不会删除该资源

### Requirement: 工作区持久记录必须用「资源身份 / ResourceIdentity + 虚拟资源地址 / VRN」引用资源，禁止持久化 real path

**归属与引用**：本 capability 的持久记录引用政策（含「位置一律以 `资源身份 / ResourceIdentity` + `虚拟资源地址 / VRN` 表达、禁持久化 `真实路径 / real path`、覆盖 API 响应体与模型可见载荷」）的正名 normative 出处为 `add-unified-virtual-resource-addressing` 的 requirement「默认寻址政策必须以 VRN 为默认形式」；「位置 vs 非位置」的判定（D-A2 可由 owner 身份推导者不落盘、D-A3 不可推导者用既有 API 端点、owner 自身运行态字段以工作区内相对路径持久化这一唯一例外）的正名出处为该 change 的 requirement「「位置」必须有可机械判定的统一判据」。本 capability 只**具名引用**上述两条 requirement，MUST NOT 复述其正文、MUST NOT 另立第二套判据。`作用域 / scope` 闭集与 `scope_id` 取值语义、VRN 语法、kind 闭集与 `拒绝码 / rejection code` 登记同样由 `add-unified-virtual-resource-addressing` change 唯一 owner 定义，本 capability 只引用。本 capability 的稳定 `resource_id` 与该身份/寻址政策不冲突，继续作为资源身份使用。

#### Scenario: 持久记录只承载身份与 VRN

- **WHEN** owner 写入一条引用外部资源的工作区持久记录
- **THEN** 该记录以 `资源身份 / ResourceIdentity` 与 `虚拟资源地址 / VRN` 指向资源；`真实路径 / real path` 只作为 owner 在本次 fs/进程调用栈内的局部变量出现，不进入任何持久化记录

#### Scenario: 需要位置时必须用 VRN 而非文件系统路径

- **WHEN** 一条持久记录需要表达资源或资源产物的所在位置
- **THEN** 该位置 MUST 编码为 `虚拟资源地址 / VRN`；MUST NOT 写入绝对路径、工作区根路径、checkpoint 文件路径、下载/截图文件路径或等价的文件系统路径

#### Scenario: 位置可由 owner 身份推导时不落盘

- **WHEN** 一条持久记录原本需要表达可由 owner 身份确定性推导的资源或资源产物位置
- **THEN** 该位置 MUST NOT 进入持久记录，改为在该次调用栈内由 owner 身份重推导；MUST NOT 写入绝对路径、工作区根路径、checkpoint 文件路径、下载/截图文件路径或等价的文件系统路径（裁定 D-A2）

#### Scenario: 位置不可推导时用既有 API 端点承载可寻址性

- **WHEN** 一条持久记录或模型可见载荷需要对外寻址一个无法由 owner 身份推导的资源产物
- **THEN** 可寻址性 MUST 由承载该能力的既有后端 API 端点引用提供；MUST NOT 为此自造 VRN kind、MUST NOT 裸拼接 VRN、MUST NOT 暴露文件系统路径（裁定 D-A3）

#### Scenario: owner 自身运行态字段按工作区内相对路径持久化

- **WHEN** 持久记录需要保存 owner 自身的运行态字段（典型为终端 shell 的当前工作目录）
- **THEN** 该字段 MUST 以工作区内相对路径持久化，MUST NOT 落盘绝对路径或工作区根路径；该字段 MUST NOT 被当作「资源所在位置」的位置引用，也不因此豁免「位置一律以 VRN 表达」；此边界只覆盖 owner 自身运行态字段，MUST NOT 扩张到任何位置表达

#### Scenario: 检出 real path 持久化即 fail-closed

- **WHEN** 一条持久化记录、一个 API 响应体或一份模型可见载荷中出现 `真实路径 / real path`
- **THEN** 系统 MUST 显式失败并判定为缺陷，MUST NOT 以脱敏、截断、`display_uri` 或默认值静默掩盖，MUST NOT 回退到「按路径查找」的旧语义

#### Scenario: 不新增第二套寻址语法

- **WHEN** 实现本 capability 的持久记录引用
- **THEN** 它 MUST 复用 `add-unified-virtual-resource-addressing` 的 VRN 语法与 `作用域 / scope` 定义，MUST NOT 定义第二套 URI 语法、scope 名或 `scope_id` 取值

### Requirement: Thread unload 和删除按资源保留范围释放资源

Thread runtime unload 或 Thread 删除 MUST 向领域 owner 发出带精确 `session_id`、`thread_id` 和 runtime generation 的幂等释放通知。领域 owner MUST 释放该 Thread 的运行期句柄和已完成操作的 operation lease，再解除该 Thread 的资源关联。对来源为该 Thread 的每项 `thread` 范围资源，owner MUST 在 unload/删除路径立即停止外部实例、核实结果并删除资源记录；这适用于当前仍关联的资源，也适用于先前被显式 detach 的资源。Thread unload 不得只卸载 Agent graph/runtime 而让临时 Browser、Terminal 或 shell 进程继续运行。`workspace` 范围资源 MUST 在来源 Thread unload、Thread 删除或来源 Session 删除后继续存在；owner 只移除对应 Thread 关联。`LifetimeScope.close()` MUST NOT 直接停止或删除外部业务资源。Thread idle 阈值仍由既有 Thread residency 能力定义；本能力另行定义的 30 分钟期限只用于回收无 Thread 关联的 `thread` 范围资源，不能代替或修改 Thread idle 阈值。

#### Scenario: Thread 释放或删除时 detach 并立即删除临时资源

- **WHEN** 创建 Browser、Terminal 或 shell 资源的 Thread runtime generation 被卸载或 Thread 被删除，且资源为 `thread` 范围
- **THEN** 领域 owner 释放该 Thread 的句柄和已完成 lease，自动移除资源关联并立即停止、核实和删除资源；不得等待 30 分钟孤儿期限，核实前不得报告已删除

#### Scenario: 临时资源清理失败时 unload 不伪报完成

- **WHEN** Thread unload 时 owner 无法结清所需 lease，或无法核实 `thread` 范围外部资源已停止
- **THEN** unload 保持阻断或明确待核实状态，资源记录仍可发现；owner 不得只 detach 后继续让进程孤立运行，也不得报告成功删除

#### Scenario: Workspace 资源不随来源 Thread 或 Session 删除

- **WHEN** 来源 Thread 被卸载或删除，或其 Session 进入删除流程，而资源已提升为 `workspace` 范围
- **THEN** owner 保留资源及其工作区记录，仅移除指向该 Thread/Session 的关联；用户仍可从工作区资源入口查询和管理该资源

### Requirement: 无关联的 Thread 范围资源在 30 分钟后自动回收

当 `thread` 范围资源的 Thread 关联数变为零时，领域 owner MUST 记录最后一次解除关联的时间并启动 30 分钟回收期限。若资源连续 30 分钟没有任何 Thread 关联，owner MUST 再次核实资源仍为 `thread` 范围、关联数为零且不存在有效 operation lease，然后停止并核实外部实例、删除资源记录。回收期限内资源 MUST 仍可通过 `resource_id` 或不可变来源 Thread 查询；API 投影 MUST 明确显示待回收状态和计划回收时间。到期前用户或来源 Thread 的工具重新 attach，或显式提升为 `workspace`，MUST 取消回收期限；仍有有效 lease 时 MUST 推迟删除，并在 lease 结束后重新核验。到期核验和删除 MUST 与 attach、提升操作串行化。`workspace` 范围资源即使长期没有 Thread 关联也 MUST NOT 自动过期。期限和回收状态 MUST 在后端重启后保持；停止或删除结果未知时 MUST 保留可发现的待核实记录，不得报告成功删除。该期限是临时资源孤儿清理策略，不是 Thread runtime 的 idle unload 阈值。

#### Scenario: 手动 detach 后连续 30 分钟无人重新关联

- **WHEN** 一个 `thread` 范围资源最后一次 Thread 关联被解除，且之后连续 30 分钟没有新的关联、也没有有效 operation lease
- **THEN** owner 自动停止并核实外部实例，然后删除资源记录；无法核实时保留明确的待核实状态

#### Scenario: 期限前重新 attach 取消孤儿回收

- **WHEN** 一个无关联 `thread` 范围资源在 30 分钟期限前被重新 attach 到其有效来源 Thread
- **THEN** owner 取消待回收状态并保留资源；后续 unload 仍按 Thread 释放流程立即删除该临时资源

#### Scenario: 到期时仍有有效操作租约

- **WHEN** 30 分钟期限到达，但资源仍有有效 operation lease
- **THEN** owner 不停止或删除资源；lease terminal 后 owner 重新检查范围、关联和期限，并在条件仍满足时完成回收

#### Scenario: 到期回收与 attach 或提升并发

- **WHEN** 资源到达 30 分钟回收期限时，attach 或提升为 `workspace` 与 owner 回收并发
- **THEN** owner 串行化两者：关联或提升先完成时取消回收；回收先进入停止流程时拒绝新关联或提升并返回明确状态

#### Scenario: Workspace 资源无关联时不按期限回收

- **WHEN** 一个 `workspace` 范围资源连续 30 分钟或更长时间没有 Thread 关联
- **THEN** owner 保留该资源；只有显式 stop/delete 才结束该资源的生命周期

#### Scenario: 后端重启不重置孤儿回收期限

- **WHEN** 后端在临时资源无关联的 30 分钟期限内重启
- **THEN** owner 恢复持久化的期限和回收状态，继续按原期限核验和清理，不从重启时刻重新计时

### Requirement: 用户可持久化并在多个 Thread 间关联工作区资源

领域 owner MUST 提供将 `thread` 范围资源提升到 `workspace` 范围的原子操作；用户或显式 Agent 工具操作可以请求提升。提升成功后资源不再依附创建它的 Thread 生命周期；原 Thread 关联可继续存在，直到 detach 或该 Thread/Session 被删除。一个 Workspace 资源 MAY 同时关联同一工作区内多个有效 `(session_id, thread_id)`；关联身份 MUST 去重并由 owner 持久化。`attach`/`detach` 专指建立/解除资源与 Thread 的业务关联，只更改关联，不复制资源、不改变保留范围、不停止外部资源，也不代表 WebSocket、浏览器客户端或终端客户端的连接/断开。`thread` 范围资源只能关联其不可变来源 Thread；其他 Thread 使用前 MUST 先提升为 `workspace` 范围。detach 目标 Thread 仍持有有效 operation lease 时 MUST 明确失败且不部分修改。解除 `thread` 范围资源的最后一项关联后，按无关联资源回收要求启动 30 分钟回收期限。

#### Scenario: 用户或 Agent 工具显式持久化 Thread 资源

- **WHEN** 用户或 Agent 工具对可持久化且状态已核实的 Thread 资源明确请求保留到工作区
- **THEN** owner 原子地将保留范围改为 `workspace`，返回完整权威资源投影，并保留现存 Thread 关联；后续来源 Thread unload 不再销毁该资源

#### Scenario: 持久化与 Thread unload 并发

- **WHEN** 用户持久化资源的操作与来源 Thread runtime unload 并发
- **THEN** owner 按其资源版本和 operation lease 串行化两项操作；最终结果只能是 Thread 范围资源已按 unload 策略核实清理，或工作区范围资源仍存活，不得出现无记录外部资源或虚假成功

#### Scenario: 同一工作区多个 Thread attach 后分别 detach

- **WHEN** 用户将一个 Workspace 资源 attach 到同工作区的两个有效 Thread，随后 detach 其中一个 Thread
- **THEN** owner 只移除指定关联，另一个 Thread 的关联及资源状态不变；detach 不关闭资源，也不关闭两个 Thread 的客户端连接

#### Scenario: Thread 范围资源不能直接转给另一个 Thread

- **WHEN** 一个 `thread` 范围资源被 attach 到来源 Thread 以外的有效 Thread
- **THEN** owner 明确拒绝操作且不改变资源范围、来源身份或关联；必须先显式提升为 `workspace` 范围

#### Scenario: detach 遇到仍在执行的资源操作

- **WHEN** 目标 Thread 对该资源仍持有有效 operation lease，用户请求 detach
- **THEN** API 返回明确的 `resource_in_use` 类错误，关联和资源状态保持不变；operation lease terminal 后用户可以重试

#### Scenario: 资源类型不支持持久化

- **WHEN** 用户试图将不支持跨 Thread 生命周期保留的资源提升到工作区范围，或 owner 无法核实其状态
- **THEN** API 明确失败，保留范围、Thread 关联和外部状态均不发生部分修改

### Requirement: stop、detach 与 delete 是不同的资源操作

`stop` MUST 由领域 owner 核实并停止外部运行资源，但保留资源记录、保留范围和 Thread 关联；`detach` 只移除一项 Thread 关联；`delete` MUST 在资源没有有效 operation lease 或 Thread 关联后，由领域 owner 核实外部资源已停止，再终结资源记录。任何操作遇到有效 lease、关联冲突或外部状态未知时 MUST 返回可识别错误或待核实状态，不得谎报成功。用户 WebSocket、SSE 或浏览器客户端连接的 attach/detach MUST 保持原有传输语义，不得映射为资源关联操作。

#### Scenario: 显式 stop 不删除持久记录

- **WHEN** 用户请求停止一个没有有效 operation lease 的 Workspace 资源
- **THEN** 领域 owner 停止并核实外部运行状态，返回完整资源投影且保留工作区记录和 Thread 关联

#### Scenario: delete 等待关联和 lease 清空

- **WHEN** 用户请求删除仍有关联 Thread 或有效 operation lease 的资源
- **THEN** owner 明确拒绝操作并返回阻断关联/lease；资源记录及外部状态保持不变

#### Scenario: 外部停止结果未知时不报告 delete 成功

- **WHEN** owner 请求删除资源但无法确认外部实例已停止或删除
- **THEN** 资源保留为 `stopping`、`unknown` 或等价的明确待核实状态，API 不返回成功删除，工作区列表仍可发现该资源

### Requirement: 工作区资源 API 提供权威查询和生命周期操作

Workspace backend MUST 在 `/api/v1/resources` 提供工作区级资源列表和详情查询，以及资源持久化、Thread attach/detach、显式 stop 和 delete 操作。列表 MUST 支持按资源类型、保留范围、当前 Thread 关联和不可变来源 `(session_id, thread_id)` 过滤；查询来源 Thread 时也 MUST 返回仍在 30 分钟宽限期内的临时孤儿资源。列表 MUST 能查询没有任何 Thread 关联的 Workspace 资源。写操作 MUST 由领域 owner 执行并返回完整权威结果；失败时返回具体错误且不返回虚假的成功状态。此 API MUST 通过 Workspace Gateway 透明代理，Gateway 不得直接读写工作区 `.boxteam/` 业务数据。

#### Scenario: 用户按 Thread 查看关联资源和临时孤儿

- **WHEN** 用户以 `session_id` 和 `thread_id` 查询 `/api/v1/resources`
- **THEN** 返回该 Thread 当前关联资源，以及在 30 分钟宽限期内已解除关联的来源资源；投影区分关联状态，并对临时孤儿显示待回收状态和计划回收时间

#### Scenario: 原 Session 不存在时仍能查询 Workspace 资源

- **WHEN** 来源 Session 已删除或当前未打开，用户查询工作区范围资源
- **THEN** API 仍返回资源 owner 的完整权威投影，不要求先解析来源 Session 或 Thread

#### Scenario: 用户执行资源生命周期操作

- **WHEN** 用户请求持久化、attach/detach、stop 或 delete
- **THEN** API 将操作交给对应领域 owner；成功时返回完整更新对象或明确删除确认，失败时返回具体错误且没有部分变更

### Requirement: Session 删除在完成前收敛 Thread 范围资源

Session 删除流程 MUST 在关闭新准入后，由各资源 owner 收敛目标 Session 内所有来源 Thread 属于该 Session 的 `thread` 范围资源，包括已手动 detach、当前没有 Thread 关联且仍处于孤儿回收期限内的资源；owner MUST 释放运行句柄和 operation lease、停止并核实外部实例后再完成 Session 的物理删除。`workspace` 范围资源不阻止删除；owner 在同一工作区资源记录中移除指向已删除 Session/Thread 的关联，并保持资源本身可查询。任何 Thread 范围资源仍有未结算 lease、停止失败或状态未知时，删除流程 MUST 保持明确的 pending/error 状态，不得把未核实资源遗留伪装成已完成清理。

#### Scenario: Session 删除清理已关联或已 detach 的临时资源并保留 Workspace 资源

- **WHEN** Session 删除流程收敛其中同时包含已关联或已 detach 的 Thread 范围资源和工作区范围资源的多个 Thread
- **THEN** owner 按不可变来源 Thread 找到并结清全部 Thread 范围资源及其 lease，再移除 Workspace 资源的 Session/Thread 关联；只有前者清理已核实且删除记录提交后，Session 才能完成物理删除

#### Scenario: 临时资源停止未知时 Session 删除暂停

- **WHEN** Session 删除期间资源 owner 无法核实一个 Thread 范围外部资源的停止结果
- **THEN** Session 保持删除中的明确状态，资源记录保留供核实/恢复，不完成目录物理删除或报告成功

### Requirement: 前端能在原 Session 生命周期之外管理 Workspace 资源

前端 MUST 提供不依赖来源 Session 存在或选中的工作区资源入口；它应按资源类型放在该资源所属的 UI 区域，而不是把工作区状态存入 Session 面板。Session 资源表面可显示当前 Thread 的关联资源和持久化快捷操作；工作区入口必须能查看无 Thread 关联的 Workspace 资源，也必须能在 30 分钟宽限期内发现来源 Thread 的临时孤儿资源、看到计划回收时间，并在适用时重新关联或提升为持久资源。界面还应提供适用的 attach/detach、stop 和 delete 操作。成功时前端 MUST 以 API 返回的完整资源投影替换本地对象；失败时 MUST 重新读取 owner 权威状态并展示明确错误。Thread 资源仍按 Session 展示，Workspace Terminal 资源在主窗口底部面板展示；其他类型遵守各自已有的资源展示区域。

#### Scenario: 用户从来源 Session 持久化资源

- **WHEN** 用户在 Session 资源区域把 Thread 资源提升到工作区范围且 API 成功
- **THEN** 前端以完整返回对象更新状态，并明确显示资源在来源 Thread unload 后仍保留于工作区

#### Scenario: 来源 Session 已删除后仍可操作资源

- **WHEN** 用户在工作区资源入口选择一个没有 Thread 关联的 Workspace 资源
- **THEN** 前端仍显示其权威状态并允许该资源支持的生命周期操作，不依赖来源 Session 的本地缓存或展示态

#### Scenario: 操作失败后前端重新同步

- **WHEN** 持久化、attach/detach、stop 或 delete 命令失败
- **THEN** 前端重新查询资源权威投影并展示后端错误，不保留只由本地乐观状态制造的成功外观

### Requirement: 领域 owner 重启后恢复或明确报告未知状态

每个领域 owner MUST 在工作区级持久存储中保留恢复资源身份、保留范围、来源 Thread、当前 Thread 关联、无关联回收期限和未收敛操作所需的事实。Workspace backend 重启后，owner MUST 按资源类型核实并恢复或重新连接外部资源；无法确认外部副作用时 MUST 暴露 `unknown`/待核实状态，不得假报停止、删除或自动重放结果未知的操作。工作区持久资源身份、保留范围及临时孤儿资源的原始回收期限 MUST 在重启后保持不变。

#### Scenario: 重启后恢复 Workspace 资源

- **WHEN** Workspace backend 重启后恢复一个 Workspace 资源
- **THEN** owner 使用原 `resource_id` 和保留范围重建可操作状态，或明确报告需要核实的状态；不会因 Thread runtime 丢失而创建重复资源

#### Scenario: 未知操作结果不被静默重放

- **WHEN** backend 在外部资源操作结果确认前崩溃，且 owner 无法证明该操作未产生副作用
- **THEN** owner 保留操作结果未知状态并要求显式核实，不自动重放可能造成重复副作用的操作
