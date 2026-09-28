## Why

工作区后端当前是**单工作区进程**：`app/container.py:348` 的 `build_app_container` 在构造期把 `workspace_root`（来自 `WORKSPACE_ROOT` 环境变量）解析为唯一根，`app/core/path_utils.py` 的 `get_workspace_root()`/`get_boxteam_root()`/`get_sessions_dir()` 直接读环境变量，`get_session_path_resolver()` 又按会话根目录做 `lru_cache` 缓存并让 SQLite catalog 连接常开。Gateway 侧则为每个工作区分派独立后端服务（`app/gateway/registry.py` 的 `workspace_id -> service_url`）。于是「当前激活工作区」成了大量代码的**隐含前提**——工作区根目录、`.boxteam/` 数据目录、会话目录解析器、进程所有权锁都默认进程内只有一个工作区。

用户已拍板允许破坏性迁移与大型改动，并允许**一个后端进程挂载多个工作区**。这要求 workspace 身份从进程级单例变成**显式、按请求传递**的寻址层身份，且必须与并行 change（统一虚拟资源寻址）的 VRN `workspace` scope **同一份身份定义**。本 change 是「workspace 身份这一层」的唯一 owner：它只引用寻址 change 的 scope / `scope_id` / 拒绝码 / 术语，不重新定义 VRN 语法。

## What Changes

- **BREAKING** 工作区后端从「单工作区进程 + `WORKSPACE_ROOT` 环境变量隐式根」改为「**已挂载工作区注册表**（进程内权威），每项含稳定 `workspace_id` 与其根目录」；工作区根目录与 `.boxteam/` 数据目录定位 MUST 由「显式 workspace_id → 注册表」得出，MUST NOT 再依赖进程级单例或激活态。
- **BREAKING** 按工作区维度操作的 HTTP API 改为**显式携带 workspace 身份**：路径段 `/api/v1/workspaces/{workspace_id}/...` 为规范载体（`X-BoxTeam-Workspace-Id` 请求头为 Gateway 内部代理层等价载体，两者必须指向同一注册表项、不得各自为政）。决策与理由见 design。
- **BREAKING** Gateway 角色变化：Gateway 仍**选**目标工作区，但目标 MUST 显式传给后端（经上述显式载体），后端 MUST NOT 猜；Gateway 不再需要为每个工作区拉起独立后端进程。
- 明确 `workspace_id` 是**寻址层身份**，与 VRN `workspace` scope 必填的 `scope_id` MUST 是同一个稳定 `workspace_id`（VRN 的 `workspace` scope 中 `scope_id` 必填且等于 workspace_id，语法本体属「统一虚拟资源寻址」change）；两处 MUST NOT 各自为政。本 change 不定义 VRN 语法，只声明 workspace 身份在寻址层的唯一来源。
- 完整化 `scope_id` 原则（权威表 R2）：VRN 的 `scope_id` 对所有 scope MUST 必填且 MUST **推导自该 scope 的稳定身份、MUST NOT 硬编码**；`workspace`→真实 workspace_id、`gateway`→真实 gateway_id、`inline`（原 `builtin`）→真实 distribution_id、`user`→`local`（单用户本地约定）。并登记两个既存违反点：`app/agents/skill_runtime.py:539` 的 `else` 分支让 `gateway` 与 `inline` 共用同一硬编码字面量 `"local"`，未分别推导 gateway_id / distribution_id。
- **接口前提**：`gateway` 身份与 workspace 身份同属**寻址层身份**，都 MUST 显式可表达；`gateway_id` MUST 真实（当前硬编码字面量 = **未满足的接口前提**，不得当作跨 gateway 寻址已成立）。同时登记 `distribution_id` 的**零生产赋值空洞**（全仓仅字段定义、resolver 读取与测试中出现，`app/container.py` 无装配），`inline` scope 的 `scope_id` 推导因此暂无真实来源。
- 进程级资源按 workspace_id 分区（清单与理由见 design）：会话目录解析器与 `lru_cache`、SQLite catalog/状态库及进程所有权锁、会话生命周期 gate/operation lease、Job 事件总线与事件通道、后台任务注册表、配置服务与 `workspace-root` 绑定、Workspace 活动/资源注册表、持久资源账本、消息流与 trace 存储。
- **BREAKING** 破坏性迁移边界（见 design）：**无存量 VRN 数据迁移** —— 权威表实测 VRN 零落盘，故只需新写字段与读路径切换；旧持久化字段中**只描述单工作区前提的非 VRN 字段**仍需显式迁移或显式失效，不回滚兼容层。
- 收口手续（具名、对称）：本 change 的 workspace 身份以 **`add-unified-virtual-resource-addressing` 的 requirement「多工作区场景下寻址层必须显式承载 scope_id 身份」** 为唯一权威定义（该 requirement 亦具名引用本 change 承接实现细节）；收口以该具名定义为准，不采用「`rg` 复核全仓」这类无目标的表述。
- 命名一致性作为跨 change 硬约束：逐字使用**冻结契约 v2** 的 `资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`，禁止同义异名。scope 闭合集与 `scope_id` 取值语义、拒绝码登记以「统一虚拟资源寻址」change 的**权威表**为准，本 change 不复述具体取值。

## Capabilities

### New Capabilities

- `multi-workspace-backend-mounting`: 一个后端进程挂载多个工作区时的进程内权威注册表、workspace 身份的显式寻址载体（路径段/头）、工作区根目录与 `.boxteam/` 定位规则、按 workspace_id 分区的进程级资源边界、Gateway 显式传目标的角色变化、破坏性迁移与回滚边界、测试影响面分层口径。

### Modified Capabilities

- `managed-backend-lifecycle`: 该 capability 现要求「Gateway 拥有默认及每个新增的本地托管工作区 runtime」，即「一个工作区一个后端进程」的隐含前提。多工作区挂载后，后端进程数与工作区数解耦，需按本 change 的注册表模型调整其「所有权/排空式重启/进程组清理」的粒度表述。

## Impact

- **规划产物**：新增本 change 的 `proposal.md` / `specs/multi-workspace-backend-mounting/spec.md` / `specs/managed-backend-lifecycle/spec.md`（delta）/ `design.md` / `tasks.md`；并按具名引用与 `add-unified-virtual-resource-addressing` 的 requirement「多工作区场景下寻址层必须显式承载 scope_id 身份」互为对称收口。
- **本轮已收口**：`add-workspace-persistent-resource-management` 的 `proposal.md`/`design.md`/`tasks.md` 与 `specs/workspace-persistent-resources/spec.md` 中「当前工作区」「`${workspace_abs_path}`」的单工作区前提已改为指向本 change 的显式 `workspace_id` 身份与已挂载工作区注册表（该 change 当时空闲，无并发编辑冲突）。
- **受影响系统（实施阶段，本 change 不写生产代码）**：`app/container.py`（`build_app_container` 单根装配 → 多工作区注册表）、`app/core/path_utils.py`（`lru_cache` 解析器与 `get_workspace_root` 系列）、`app/main.py`（`WORKSPACE_ROOT` 启动前提）、`app/api/**`（受影响路由的 workspace 身份载体）、`app/core/sqlite_state.py` 的 `SQLiteProcessOwnership`、`app/services/**` 中按工作区构造的服务/注册表/总线、`app/gateway/registry.py` 与 `app/gateway/server/workspace_proxy.py`（目标显式传递）、已提交的 OpenAPI 快照。
- **破坏性**：既有「一个后端进程一个工作区」的部署形态、只描述单工作区前提的**非 VRN** 持久化字段、以「当前激活工作区」为隐含前提的客户端调用一并收敛；不提供旧形态兼容层或双读。
- **不涉及存量 VRN 数据迁移**：权威表实测 VRN 零落盘（157 live + 44 dev/temp SQLite 对 `boxteam://` 零命中、无 `resource_activation*` 表；`display_uri` 仅测试写入、container 无装配、生产 seal 恒为 `None`），故本 change 的 workspace 身份显式化只需新写字段与读路径切换。
- **契约依赖**：VRN 语法、scope 闭合集、`scope_id` 取值语义与拒绝码登记由「统一虚拟资源寻址」change 独占，本 change 只引用其权威表；会话上下文资源的承载由「会话上下文 URI 统一改造」change 负责。
