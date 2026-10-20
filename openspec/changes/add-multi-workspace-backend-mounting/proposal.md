## Why

原规划基线中的工作区后端是**单工作区进程**：`build_app_container` 在构造期把 `WORKSPACE_ROOT` 解析为唯一根，路径工具直接读环境变量，会话目录解析器又按会话根目录缓存 SQLite catalog 连接。Gateway 侧则为每个工作区分派独立后端服务。于是「当前激活工作区」成了大量代码的**隐含前提**——工作区根目录、`.boxteam/` 数据目录、会话目录解析器、进程所有权锁都默认进程内只有一个工作区。当前已进入用户授权的实施阶段；部分注册表、装配和显式路由已集成，是否完成以本 change 的逐项实现及验收证据为准，不能从这段历史基线重新派发整套改造。

用户已拍板允许破坏性迁移与大型改动，并允许**一个后端进程挂载多个工作区**。这要求 workspace 身份从进程级单例变成**显式、按请求传递**的寻址层身份，且必须与并行 change（统一资源寻址）的 URI `workspace` scope **同一份身份定义**。本 change 是「workspace 身份这一层」的唯一 owner：它只引用寻址 change 的 scope / `scope_id` / 拒绝码 / 术语，不重新定义 URI 语法。

## What Changes

- **BREAKING** 工作区后端从「单工作区进程 + `WORKSPACE_ROOT` 环境变量隐式根」改为 generation-local 已挂载 workspace runtime 注册表；每项按稳定 `workspace_id` 挂载对应工作区根。物理根的持久化来源只有负责原生 target 的 Gateway 唯一 registry；后端 runtime 注册表仅从该绑定构造，不持久化第二份根事实。
- 明确物理 workspace root 的唯一 owner：Gateway registry 将用户登记的根与稳定 Gateway target ID、Backend `workspace_id` 绑定，并将该绑定作为启动输入；managed Backend group 只存成员身份与 generation，不复制 root。本机现有 managed token 代表本 Gateway 管理权限；远端注册、替换、移除须由原生目标 Gateway 的现有 `permissions.federation` policy 以 `managed_workspace_admin` 显式授权，并按认证 peer 派生 `principal_ref_for_gateway`。授权按目标 Gateway 生效，不依赖首次创建前尚不存在的 `workspace_id`。保留外部工作区注册与冷启动；中继只转送，不持久化根、不作最终授权；默认通信 allow、配对和读取权限均不授予管理权。
- **BREAKING** 按工作区维度操作的 HTTP API 改为**显式携带 workspace 身份**：路径段 `/api/v1/workspaces/{workspace_id}/...` 为规范载体（`X-BoxTeam-Workspace-Id` 请求头为 Gateway 内部代理层等价载体，两者必须指向同一注册表项、不得各自为政）。决策与理由见 design。
- **BREAKING** Gateway 角色变化：Gateway 仍**选**目标工作区，但目标 MUST 显式传给后端（经上述显式载体），后端 MUST NOT 猜；Gateway 不再需要为每个工作区拉起独立后端进程。
- 明确 `workspace_id` 是**寻址层身份**，与 URI `workspace` scope 必填的 `scope_id` MUST 是同一个稳定 `workspace_id`（URI 的 `workspace` scope 中 `scope_id` 必填且等于 workspace_id，语法本体属「统一资源寻址」change）；两处 MUST NOT 各自为政。本 change 不定义 URI 语法，只声明 workspace 身份在寻址层的唯一来源。
- `scope_id` 的完整身份原则、scope 闭合集和取值表仅引用 `add-unified-virtual-resource-addressing` requirement「scope 必须取自定稿闭集且 scope_id 对所有 scope 必填」及该 owner 的 gateway/inline 身份来源 requirements。本 change 只落实显式 HTTP workspace_id 与工作区 URI scope_id 同源，不复制其它 scope 的取值规则；原硬编码修复和装配证据保留在 tasks 1.6。
- 进程级资源按 workspace_id 分区（清单与理由见 design）：会话目录解析器与 `lru_cache`、SQLite catalog/状态库及进程所有权锁、会话生命周期 gate/operation lease、Job 事件总线与事件通道、后台任务注册表、配置服务与 `workspace-root` 绑定、Workspace 活动/资源注册表、持久资源账本、消息流与 trace 存储。
- **BREAKING** 破坏性迁移边界（见 design）：按用户授权让旧开发运行数据显式失效并清理，只保留新写字段与单一读路径，不增加存量 URI 扫描、回填或兼容层；只描述单工作区前提的旧字段不再读取。当前正确的新 writer 及精确恢复合同仍须验收。
- 收口手续（具名、对称）：本 change 的 workspace 身份以 **`add-unified-virtual-resource-addressing` 的 requirement「多工作区场景下寻址层必须显式承载 scope_id 身份」** 为唯一权威定义（该 requirement 亦具名引用本 change 承接实现细节）；收口以该具名定义为准，不采用「`rg` 复核全仓」这类无目标的表述。
- 命名一致性作为跨 change 硬约束：逐字使用**冻结契约 v2** 的 `资源身份 / ResourceIdentity`、`资源 URI`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`，禁止同义异名。scope 闭合集与 `scope_id` 取值语义、拒绝码登记以「统一资源寻址」change 的**权威表**为准，本 change 不复述具体取值。

## Capabilities

### New Capabilities

- `multi-workspace-backend-mounting`: 一个后端进程挂载多个工作区时的 generation-local runtime 注册表、Gateway 唯一 workspace-root binding 与冷启动/管理授权输入（本地 managed token、远端 federation `managed_workspace_admin`）、workspace 身份的显式寻址载体（路径段/头）、`.boxteam/` 定位规则、按 workspace_id 分区的进程级资源边界、Gateway 显式传目标的角色变化、破坏性迁移与回滚边界、测试影响面分层口径。

### Modified Capabilities

- `managed-backend-lifecycle`: 共享 Backend 的唯一 owner 持进程句柄/generation/完整启动清单；工作区成员保独立业务身份与辅助服务。明确完整组 generation/成员前置条件、排空/force、不可变 mount 集合切换、一次清理/接管/恢复与配置候选补偿，统一影响面 DTO及Web消费；普通新增工作区仍独立singleton启动，外部后端仍仅探测。

## Impact

- **实施合同**：本 change 的 proposal、specs、design 和 tasks 共同约束实现，并按具名引用与 `add-unified-virtual-resource-addressing` 的 requirement「多工作区场景下寻址层必须显式承载 scope_id 身份」互为对称收口。
- **本轮已收口**：`add-workspace-persistent-resource-management` 的 `proposal.md`/`design.md`/`tasks.md` 与 `specs/workspace-persistent-resources/spec.md` 中「当前工作区」「`${workspace_abs_path}`」的单工作区前提已改为指向本 change 的显式 `workspace_id` 身份与已挂载工作区注册表（该 change 当时空闲，无并发编辑冲突）。
- **受影响系统**：`app/container.py`（单根装配 → 多工作区 runtime 注册表）、`app/core/path_utils.py`（解析器与工作区路径 owner）、`app/main.py`（挂载和生命周期）、`app/api/**`（workspace 身份载体，普通 Workspace DTO 不暴露物理根）、`app/core/sqlite_state.py` 的 `SQLiteProcessOwnership`、`app/services/**` 中按工作区构造的服务/注册表/总线、Gateway 唯一 root registry 与 workspace proxy（目标显式传递；root 不进入资源投影）、现有 `permissions.federation` schema/policy owner 及其全部注册调用方、OpenAPI 快照及相关测试。
- **破坏性**：既有「一个后端进程一个工作区」的部署形态、只描述单工作区前提的**非 URI** 持久化字段、以「当前激活工作区」为隐含前提的客户端调用一并收敛；不提供旧形态兼容层或双读。
- **不涉及旧开发数据迁移**：原零 URI 取证属于历史基线；A01 `16f09ee7` 已将真实 activation freeze/seal/restore 接入生产，不能再用“无装配、seal 恒为空”推断当前状态。本 change 按用户清理授权完成显式身份的新写字段与单一读路径，不从旧数据反推默认工作区或当前来源。
- **契约依赖**：URI 语法、scope 闭合集、`scope_id` 取值语义与拒绝码登记由「统一资源寻址」change 独占，本 change 只引用其权威表；会话上下文资源的承载由「会话上下文 URI 统一改造」change 负责。

未注册目录不是本 capability 的 mount 或工作区注册，不得伪造 workspace_id 或增加 WorkspaceTarget。其 URI、唯一原生 Gateway 控制面目录根绑定及路径持久化例外只引用 `add-unified-virtual-resource-addressing` requirement「原生目录根绑定必须由目标 Gateway 唯一持有」，不改变本 capability 的 workspace-root binding 合同。
