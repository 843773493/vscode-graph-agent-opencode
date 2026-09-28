## 1. 已挂载工作区注册表与身份层

- [ ] 1.1 引入进程内权威的「已挂载工作区注册表」：每项含稳定 `workspace_id`（严格 UUID 文本，复用 `app/core/workspace_identity.validate_workspace_id`）与其根目录；提供按 `workspace_id` 的精确查找与「未登记即 fail-closed」行为，不提供默认工作区回退。
- [ ] 1.2 把工作区根目录与 `.boxteam/` 数据目录定位从 `app/core/path_utils.py` 的环境变量单例改为「显式 `workspace_id` → 注册表」；`get_workspace_root()`/`get_boxteam_root()`/`get_sessions_dir()` 不再作为业务解析入口（保留或删除由实施时确认调用方全量平移后决定）。
- [ ] 1.3 把 `get_session_path_resolver()`/`get_session_creation_service()` 的 `lru_cache` 键从「会话根目录」改为 workspace 维度；每工作区独立 catalog 连接与 `SQLiteProcessOwnership` 锁。
- [ ] 1.4 确认 workspace_id 只有**一个**命名空间：寻址层统一使用后端身份 UUID；`app/gateway/workspace_ids.py` 的 `gw_` ID 若保留，MUST 只作 Gateway 控制面内部标识，不进入工作区寻址。且 MUST 与 VRN `workspace` scope 必填 `scope_id` 取值同源（同一个稳定 workspace_id）。
- [ ] 1.5 落实 `scope_id` 推导原则（权威表 R2）：`scope_id` MUST 推导自该 scope 的稳定身份、MUST NOT 硬编码；`workspace` scope 取真实 workspace_id，`gateway` scope 取真实 gateway_id，`inline`（原 `builtin`）scope 取真实 distribution_id（来源与编码已由「统一虚拟资源寻址」change 定稿为 manifest 的 `distribution` + `version`，此处只引用）。
- [ ] 1.6 登记两处既存硬编码违反点并定界：`app/agents/skill_runtime.py:539` 的 `else` 分支对 `gateway` 与 `inline`（原 `builtin`）共用同一字面量 `"local"`；`distribution_id` 全仓零生产赋值（`app/container.py` 无装配）、`ResolutionContext` 生产侧零构造。**来源已裁定**：`distribution_id` 由发行包 runtime manifest（`packages/launcher/runtime-manifest.schema.json`）的 `distribution` + `version` 推导，编码规则与缺失 fail-closed 见「统一虚拟资源寻址」change 的 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」；本 change 只登记为接口前提，装配由该 change 实施。
- [ ] 1.7 全仓 `rg` 复核「当前激活工作区 / active workspace / WORKSPACE_ROOT / 进程级单根」全部调用方，形成平移清单，确认无遗留悬空调用。

## 2. 服务图按 workspace_id 分区

- [ ] 2.1 把 `app/container.py` 的 `build_app_container` 从「构造期单根装配」改为「持有注册表 + 按 `workspace_id` 惰性构造并缓存服务图」；同 `workspace_id` 只构造一次（进程内幂等）。
- [ ] 2.2 逐项把设计 D3/D4 与 spec「进程级资源必须按 workspace_id 分区」列举的资源改为按工作区分区：配置服务及其工作区根绑定、工作区活动/资源注册表、持久资源账本、会话生命周期 gate 与 operation lease、Job 事件总线与事件通道、后台任务注册表、消息流与 trace 存储。
- [ ] 2.3 校验分区后的隔离：为同一进程挂载两个工作区，断言 A 的会话/事件/任务/SQLite 状态不出现在 B 的请求结果中，且两工作区锁互不阻塞。

## 3. API 契约的破坏性迁移

- [ ] 3.1 按设计 D1 引入路径段载体 `/api/v1/workspaces/{workspace_id}/...`；保留 `X-BoxTeam-Workspace-Id` 作为 Gateway 代理层等价载体，并实现「两载体不一致即显式失败」。
- [ ] 3.2 实现「缺失 workspace 身份即显式拒绝」：不复用激活态、不使用默认工作区补齐。
- [ ] 3.3 更新 Gateway：仍负责选目标，但 MUST 显式把目标传给后端；移除「每个工作区一个后端进程」的默认假设，使一个后端进程可服务多个工作区。跨 gateway 时**网关授权段 / gateway authority**承载稳定 gateway_id，拓扑为**星型解析 / star-topology resolution**（hub-spoke）；解析命中只返回稳定**资源身份 / ResourceIdentity**与内容、不返回 locator，不可解析 fail-closed；上界（visited set / max_transit_gateways=1 / max_gateway_hops=2 / 总 deadline）作为显式策略常量。解析链本体引用「统一虚拟资源寻址」change。
- [ ] 3.4 校验 gateway 身份的接口前提：`gateway` scope 的 `scope_id` MUST 是**真实 gateway_id**；**来源与注入 owner 已裁定**——owner = Gateway 侧按请求注入，取值按「统一虚拟资源寻址」change 的 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」推导（本 change 只引用、不复述取值规则），剩通道与装配；在装配完成前当前硬编码字面量形态 MUST 仍视为**未满足的接口前提**，MUST NOT 被当作跨 gateway 寻址已成立；`gateway` 身份与 workspace 身份同属寻址层身份、都必须显式可表达。
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
