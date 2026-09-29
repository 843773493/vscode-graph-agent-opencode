## 背景

动机和行为约定分别见 `proposal.md` 与 `specs/workspace-persistent-resources/spec.md`。现有生命周期方案已明确：Thread 常驻管理方决定何时卸载运行时，`LifetimeScope` 释放进程内资源，持久操作租约表示外部操作占用，浏览器（Browser）、终端（Terminal）等领域管理方核实外部资源状态。新的持久化边界还必须跨越 Session 删除，因此资源权威记录不能只保存在 Session 节点下。

当前浏览器和终端资源界面围绕 Session 组织。传输层客户端的连接与断开描述 WebSocket 或浏览器连接，不代表资源的持久归属。现有 `turn-message-stream` 约定仍将资源停止权交给通用 `ResourceManager`/`cleanup_policy`；本变更将这一职责交由实际领域管理方承担。

## 目标与非目标

**目标：**

- 由每个领域管理方在工作区范围内持有该资源唯一的持久记录，记录资源身份、保留范围、Thread 关联、操作租约与经核实的外部状态。
- 让用户明确把 Thread 创建的资源提升为工作区范围，并在来源 Thread 或 Session 不存在后仍能查找、关联、停止或删除资源。
- Agent 创建的 Browser、Terminal 等外部资源默认临时；Thread 卸载或删除时自动解除关联并立即删除临时资源。手动解除关联后无人重新关联的临时资源在 30 分钟后自动回收，避免 Agent 遗漏删除。
- Thread 卸载、Thread 删除和 Session 删除时通知相应的领域管理方；只有所需的 Thread 范围清理得到核实时，相关卸载或删除才能完成。
- 通过工作区 API 和资源类型所属的界面区域展示领域管理方的权威投影并执行操作。

**非目标：**

- 不定义 Thread 身份、计算图加载或复用，也不定义 Thread runtime 的 30 分钟空闲卸载策略；这些由 `add-itemized-rollout-context` 管理。本文定义的 30 分钟只用于回收脱离所有 Thread 的临时资源。`add-context-injection-lifecycle` 负责 `LifetimeScope` 的进程内释放和上下文状态保留。
- 不实现长期运行工具任务或 Thread 邮箱的恢复；这些机制的持久化归属与恢复约定由 `add-itemized-rollout-context` 管理。本变更只使用其中的任务租约与精确 Thread 身份。外部副作用结果未知时，本变更不重放资源操作。
- 不新建另一份资源状态注册表，也不允许 Gateway 直接访问工作区 `.boxteam/` 业务数据。

## 决策

### 1. 领域管理方保存资源事实，注册表只提供投影

每个现有浏览器、终端或其它资源领域管理方都在对应工作区 `${workspace_abs_path}/.boxteam/` 下的持久存储中保存自己的资源记录。记录负责维护 `resource_id`、资源类型、`retention_scope`、不可变的来源 `(session_id, thread_id)`、当前 Thread 关联集合、经核实的生命周期状态，以及操作租约和对账记录的引用。来源 Thread 用于释放和对账，不等于当前关联；资源解除关联后仍能被来源 Thread 的卸载和 Session 删除流程找到。Session 显示名和 Session 物理路径都不能作为资源身份。`SessionResourceProviderRegistry` 汇总各管理方提供的查询结果并路由命令；它不得缓存另一份生命周期状态，也不得建立第二份关联账本。


`${workspace_abs_path}` 与来源 `(session_id, thread_id)` 所属工作区 MUST 由显式 `workspace_id` 解析，其身份定义与解析规则以 `add-multi-workspace-backend-mounting` change 的**已挂载工作区注册表**为准（一个后端进程 MAY 挂载多个工作区）；MUST NOT 依赖「当前激活工作区」，MUST NOT 用进程级单根推断。跨工作区资源共享时，MUST 使用寻址层的显式 `workspace_id` 而非任何进程激活态。

考虑过把记录留在来源 Session、需要时再复制到另一个目录，或让注册表成为中央管理方。前者无法可靠跨越递归 Session 删除，还会形成两个状态源；后者会把不同领域的外部状态和停止规则重复写入注册表。由实际领域管理方持有工作区范围记录，能保留单一权威，同时允许上层组合带类型的查询结果。

### 2. 分别记录资源保留范围与 Thread 关联

Browser、Terminal 等 Agent 资源创建工具必须显式提供 `retention_scope` 参数，并将默认值设为 `thread`，表示非持久资源。只有用户选择或工具参数明确指定 `workspace` 时，资源才以工作区持久范围创建。用户或显式工具操作也可以将已有资源原子地提升为 `workspace`；提升不更换 `resource_id`，也不复制外部资源。Thread 范围资源只关联创建它的 `(session_id, thread_id)`；工作区资源可以关联同一工作区内零个或多个有效 Thread。持久关联只是业务引用，不表示客户端连接，也不会隐式持有运行时租约。

领域管理方根据资源版本号和有效操作租约串行处理提升与生命周期操作。若目标 Thread 仍持有该资源的操作租约，`detach`（解除关联）必须失败且不得部分修改。建立关联前必须验证目标 Thread 是同一工作区内的有效 Thread；Thread 范围资源只能重新关联到其来源 Thread，跨 Thread 关联前必须先提升为 `workspace`。某一 Thread 持有的操作租约不因另一个 Thread 的关联增删而改变。

考虑过把持久化实现为 Session 资源上的布尔值，或让 `attach` 同时转移资源所有权。这两种方式都会混淆资源寿命与当前使用者，使并发使用和删除难以判定。通过唯一的 `retention_scope` 参数显式选择范围，并分别记录保留范围和 Thread 关联，能直接表达两者的独立变化，同时让 Agent 工具默认创建临时资源。

### 3. `LifetimeScope` 通知卸载，由领域管理方停止外部资源

Thread 常驻机制沿用既有的空闲卸载资格和代际栅栏。卸载或显式删除 Thread 时，向每个领域管理方发送身份由 `(session_id, thread_id, runtime_generation)` 确定的幂等通知。领域管理方先释放该 Thread 的运行句柄和已完成操作的租约，再移除该 Thread 的资源关联。对 `thread` 范围资源，owner 会按不可变来源 Thread 找到全部资源，随后立即执行删除流程：停止外部实例、核实结果并删除资源记录；不得只卸载 Agent 计算图或运行时，也不得解除关联后让浏览器、终端进程继续运行。对 `workspace` 范围资源，owner 只移除该 Thread 的关联并保留资源。`LifetimeScope.close()` 只释放进程内句柄，不能代替领域管理方对外部资源停止和删除的回执。

Session 删除复用现有 `Session catalog` 与栅栏协议：关闭新准入后，删除流程要求领域管理方清理 Thread 范围资源，并等待管理方回执，再进行物理删除。工作区范围记录保留在工作区存储中，各管理方只删除指向该 Session/Thread 的引用。清理结果不确定时，删除保持 `pending` 状态或返回管理方错误，由现有删除日志恢复。

考虑过由 Thread 作用域直接停止资源，或把所有资源一律改成持久资源。前者绕过领域管理方，可能关闭共享的工作区资源；后者会让临时上下文无限保留。实际资源管理方能根据自己的权威记录区分这两种情况。

### 4. `stop`、解除关联与 `delete` 是不同操作

`attach`/`detach` 只表示建立或解除资源与 Thread 的业务关联，不表示浏览器、终端、WebSocket 或 SSE 客户端的连接状态。手动 `detach` 只删除 Thread 关联，不会停止资源；若资源是 `thread` 范围且关联变为零，owner 持久记录 `unattached_since`，开始 30 分钟孤儿回收期限。期限内仍有效的来源 Thread 可以重新关联资源，此时取消回收计时；跨 Thread 重新关联必须先提升为 `workspace`。持续无关联满 30 分钟后，owner 再次确认保留范围、关联和操作租约，并自动停止、核实和删除该资源。`workspace` 范围资源即使没有 Thread 关联也永不过期。

Thread unload 是独立的、立即执行的清理路径：对临时资源，owner 在卸载事件中先解除关联，再立即执行删除；不等待 30 分钟孤儿期限。孤儿期限只是防止显式 `detach` 后遗漏 `delete` 的兜底。计时起点和期限必须持久化，后端重启后继续生效。

`stop` 结束外部资源的运行状态，但保留管理方记录、保留范围和 Thread 关联。显式 `delete` 要求资源没有关联或有效操作租约，然后由领域管理方停止并核实外部实例，最后才将资源记录标记为终结或删除。孤儿自动回收与 Thread unload 都复用这项 owner 删除语义。如果资源仍有关联、租约，或外部状态未知，操作必须返回具体阻断原因或明确的 `pending` 状态。

回收计时到期时，owner 必须在串行化边界内重新核对资源仍为 `thread` 范围、关联数仍为零、操作租约已结清，并且期限确已到达。并发 `attach` 或提升若先完成，则取消回收；若回收已进入停止流程，则必须拒绝新的关联并返回明确状态。停止结果未知或删除失败时，保留可发现的待核实记录，不得伪报成功。

### 5. API 与界面使用完整的领域管理方投影

工作区后端通过 `/api/v1/resources` 提供列表和详情查询，以及由领域管理方执行的提升、建立关联、解除关联、停止、删除命令。创建资源的 Agent 工具必须使用默认 `retention_scope=thread`，并允许显式选择 `workspace`。投影至少包含稳定身份、资源类型、保留范围、来源 Thread、当前关联、已核实的生命周期状态和可用操作；Thread 范围资源零关联时还显示待回收状态和计划回收时间。如果注册表查询领域管理方时发生错误，必须返回错误或明确标记结果不完整，不能伪装成空列表。Gateway 只透明代理 API，不读取工作区业务数据。

用户可在工作区范围的资源入口中管理资源，包括没有任何 Thread 关联的记录，也包括仍在 30 分钟宽限期内的临时孤儿资源。Session 资源界面展示当前 Thread 的关联资源，并可提供提升或解除关联的快捷操作。工作区终端资源显示在主窗口底部面板；其它类型继续显示在各自所属的资源区域。前端操作成功时用 API 返回的完整管理方投影替换本地对象，失败时重新读取权威状态。

考虑过只提供 Session 范围控件，或将所有资源放进一个混合面板。只提供 Session 控件会让失去来源 Session 的工作区资源无处可找；混合面板则会违反现有资源区域归属。把工作区入口放入每种资源既有的展示区域，既能找到资源，也不会把工作区状态错误地放进 Session 面板。

### 6. 一次性迁移既有资源事实，不保留双重权威来源

如果既有管理方把资源身份或生命周期状态保存在 Session 下，版本化迁移会把管理方的权威记录和已核实的外部身份迁移到新的工作区级存储，并将 Session 节点改为只保存引用。迁移保留 `resource_id` 和来源 Thread，依据管理方记录还原当前 Thread 关联，不启动、停止或复制外部资源。对已无 Thread 关联的临时资源，只有在迁移数据提供可信的最后解除关联时间时才沿用该时间；否则从迁移提交时间开始计算 30 分钟宽限期。状态或来源无法核实的资源标记为 `unknown`/`blocked`，在对账完成前不得自动删除。迁移只能使用已知的管理方记录和生命周期准入栅栏，不得扫描目录猜测资源。

切换完成后，生产读写只使用工作区级管理方记录，不保留双读、双写或 Session 范围回退逻辑。若回滚到不认识新数据格式的程序版本，必须在修改这些记录前停止；不能为了恢复旧的 Session 范围行为而删除工作区资源。

### 7. 持久记录只承载身份与 VRN，逐条消除已确证的 real path 持久化

本 change 的工作区持久记录、API 响应体与模型可见载荷 MUST 只承载 `资源身份 / ResourceIdentity` 与 `虚拟资源地址 / VRN`（必要时加并列 revision 字段），MUST NOT 承载 `真实路径 / real path`。该 prohibition 与 `add-unified-virtual-resource-addressing` 的 requirement「三层职责必须严格分离」同源；{scope} 闭集、`scope_id` 取值、VRN 语法、kind 与 `拒绝码 / rejection code` 均引该 change，本 change 不复述、不自造。

**已确证与「real path 不持久化」冲突的字段与行为（逐条给出修正方向）**：

1. 终端 owner 的持久状态文件把 `cwd`（真实绝对路径）写进每条终端记录：`src/workspace-services/terminal/server/terminalSession.js` 的 `toRecord()` 返回 `cwd: this.cwd`，经 `terminalManager.js` 的 `stateStore.write({ workspace_id, workspace_root, terminals })` 落到 `.boxteam/terminal-manager/terminals.json`。**修正方向**：终端工作目录是 owner 自身的运行态字段，按规范化边界不属于「资源所在位置」，因此落盘为工作区内相对路径 `cwd_relative`（`"."` 表示工作区根），绝对路径只在 PTY 启动调用栈内由工作区根重推导；不为此自造 VRN kind。
2. 同一 owner 的持久状态文件顶层还写 `workspace_root`（工作区真实根路径）。**修正方向**：工作区身份改用 `add-multi-workspace-backend-mounting` 的显式 `workspace_id`；`workspace_root` 不得落盘。
3. 浏览器 owner 的持久状态文件把 checkpoint 真实文件路径写进资源记录：`browserSession.js` 的 `this.record.checkpoint = { path: checkpointWrite.path, ... }`，而 `checkpointWrite.path` 来自 `browserStateStore.js` 的 `filePath = path.join(this.checkpointDir, ...)`，经 `session.snapshot()` 落到 `.boxteam/browser-manager/browsers.json`。**修正方向**：checkpoint 是资源产物位置，位置可由 owner 身份（`browser_id`）确定性推导，故该位置 MUST NOT 落盘（裁定 D-A2）；文件路径只在同一次读取/写入调用栈内重推导。
4. 浏览器 owner 的下载记录把真实文件绝对路径写进资源记录：`browserStateStore.js` 的 `writeDownload` 返回 `path: filePath`，由 `browserSession.js` 追加到 `this.record.downloads`（上限 50 条）并持久化；读取侧又把它当可解析路径消费。**修正方向**：下载产物位置由 `(browser_id, download_id, filename)` 在调用栈内重推导（裁定 D-A2）；越界校验改为在调用栈内由重推导出的 real path 上完成，不得把 real path 当作持久字段。
5. 上述真实路径经投影上浮到 API 与模型可见载荷：`app/services/mapping/session_resource_mapper.py` 把 `cwd` 塞进 `SessionResourceDTO.metadata`、把 `checkpoint`（含 `path`）透传，`/{session_id}/resources` 响应体因此携带 real path；`browserSession.js` 的截图工具返回 `image_path`（`writeScreenshot` 产出的真实路径）。**修正方向**：投影层一律不承载 real path——终端 `cwd` 收窄为不含路径的 `cwd_relative`、checkpoint 收窄为显式布尔 `checkpoint_available`；截图产物无法由 owner 身份推导且必须对外可寻址，改用同族既有只读端点 `/api/browsers/{browser_id}/screenshots/{screenshot_id}` 引用（裁定 D-A3），不为此自造 VRN kind。本 change 的 `/api/v1/resources` 投影与模型可见载荷 MUST NOT 承载 real path。

**已登记的行为变更（非零回归）**：终端工作目录改存相对路径后，工作区被移动时 restore 会把相对路径重锚定到新根，而旧实现落盘的绝对路径会指向旧位置。这是有意且有方向正确的行为变更，本节在此显式登记，不再声称「搬迁前后逐字节相同」；相对子目录、越界绝对路径（按 `path.relative` 的 `..` 表达保留原语义）与符号链接三类与旧实现逐字节等价。**一次性迁移**：对既有只有绝对 `cwd`、没有 `cwd_relative` 的旧持久记录，owner 在 load 时由工作区根推导相对路径并物理写回；推导失败（非字符串/空串、非绝对路径、目录不存在）MUST fail-closed 并给出可定位错误，绝不静默丢弃终端的记录、绝不回退进程 cwd。

**经核实的正面结论（避免为收严而收严）**：本 change 的稳定 `resource_id` **不是** real path（`session_resource_mapper.py:21/80/156` 分别取 `handle.task_id`、`terminal_id`、`browser_id`，即 owner 生成的不透明 ID），故 proposal 层「`resource_id` 与身份/寻址政策不冲突」成立；收严不是纠正 `resource_id`，而是把这些**既存真实路径字段**纳入规范层 prohibition，使其从「proposal 意图」升格为可验证义务。

**边界**：本 change 只新增约束与引用，不扩大既有能力边界；具体字段改名、VRN 化与投影清理的实现归属见 tasks 第 6 节，取值来源一律引 `add-unified-virtual-resource-addressing`。

## 风险与权衡

- [列表、卸载或删除期间领域管理方不可用] → 返回明确的管理方错误；需要清理时保持卸载/删除处于 `pending` 状态，不得把资源显示为不存在或已成功停止。
- [提升、重新关联与孤儿回收竞争] → 通过领域管理方的资源版本号、租约和持久化期限串行处理；到期前重新关联或提升则取消回收，回收先进入停止流程则拒绝新关联。
- [Agent 只执行 detach 而遗漏 delete] → 领域管理方对零关联的 Thread 范围资源记录最后解除关联时间，连续 30 分钟后自动停止、核实并删除；Thread unload 则立即清理，不等待该期限。
- [后端在孤儿期限内重启] → 持久化零关联起始时间并恢复待办回收；重启不会重置 30 分钟，也不扫描磁盘猜测资源。
- [工作区资源失去来源 Session 关联] → 提供独立的工作区列表和资源类型入口；关联是可删除的引用，不是查找资源的唯一途径。
- [既有 Session 资源状态无法核实] → 保留可见的 `blocked`/`unknown` 迁移状态并要求管理方对账；不静默丢弃状态，也不伪造外部资源。

## 迁移计划

1. 为参与的资源领域管理方增加工作区级权威记录，以及来源 Thread、当前关联、孤儿期限和操作索引；保留稳定的 `resource_id` 并核实当前外部身份。
2. 使用现有工作区/Session 生命周期准入栅栏，将已知的 Session 资源记录迁入新存储，并把 Session 侧重复详情改为引用。对无可靠解除关联时间的临时孤儿，从迁移提交时开始计算 30 分钟宽限期；启用工作区范围提升前，必须处理完所有记录。
3. 修改卸载和 Session 删除的接入方式，使其发送幂等的管理方通知，并要求 Thread 范围清理得到回执；移除由 `cleanup_policy` 或通用停止器直接决定是否停止的逻辑。
4. 增加工作区 API 和资源类型所属的前端入口；客户端操作成功时整体替换完整返回对象，失败时重新读取权威状态。
5. 所有调用方切换到新的管理方记录后，删除旧的 Session 范围读写链路。若部署中断，依据迁移日志保留旧记录或唯一的新权威记录；不得双写，也不得删除未经核实的外部资源。

## 待确认问题

无。资源创建默认范围、领域管理方职责、Thread 卸载清理、孤儿回收期限、API 操作、界面入口和删除行为均已确定。
