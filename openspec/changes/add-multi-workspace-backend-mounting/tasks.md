## 1. 已挂载工作区注册表与身份层

- [ ] 1.1 引入进程内权威的「已挂载工作区注册表」：每项含稳定 `workspace_id`（严格 UUID 文本，复用 `app/core/workspace_identity.validate_workspace_id`）与其根目录；提供按 `workspace_id` 的精确查找与「未登记即 fail-closed」行为，不提供默认工作区回退。
- [ ] 1.2 把工作区根目录与 `.boxteam/` 数据目录定位从 `app/core/path_utils.py` 的环境变量单例改为「显式 `workspace_id` → 注册表」；`get_workspace_root()`/`get_boxteam_root()`/`get_sessions_dir()` 不再作为业务解析入口（保留或删除由实施时确认调用方全量平移后决定）。
- [ ] 1.3 把 `get_session_path_resolver()`/`get_session_creation_service()` 的 `lru_cache` 键从「会话根目录」改为 workspace 维度；每工作区独立 catalog 连接与 `SQLiteProcessOwnership` 锁。
- [ ] 1.4 确认 workspace_id 只有**一个**命名空间：寻址层统一使用后端身份 UUID；`app/gateway/workspace_ids.py` 的 `gw_` ID 若保留，MUST 只作 Gateway 控制面内部标识，不进入工作区寻址。且 MUST 与 VRN `workspace` scope 必填 `scope_id` 取值同源（同一个稳定 workspace_id）。
- [ ] 1.5 全仓 `rg` 复核「当前激活工作区 / active workspace / WORKSPACE_ROOT / 进程级单根」全部调用方，形成平移清单，确认无遗留悬空调用。

## 2. 服务图按 workspace_id 分区

- [ ] 2.1 把 `app/container.py` 的 `build_app_container` 从「构造期单根装配」改为「持有注册表 + 按 `workspace_id` 惰性构造并缓存服务图」；同 `workspace_id` 只构造一次（进程内幂等）。
- [ ] 2.2 逐项把设计 D3/D4 与 spec「进程级资源必须按 workspace_id 分区」列举的资源改为按工作区分区：配置服务及其工作区根绑定、工作区活动/资源注册表、持久资源账本、会话生命周期 gate 与 operation lease、Job 事件总线与事件通道、后台任务注册表、消息流与 trace 存储。
- [ ] 2.3 校验分区后的隔离：为同一进程挂载两个工作区，断言 A 的会话/事件/任务/SQLite 状态不出现在 B 的请求结果中，且两工作区锁互不阻塞。

## 3. API 契约的破坏性迁移

- [ ] 3.1 按设计 D1 引入路径段载体 `/api/v1/workspaces/{workspace_id}/...`；保留 `X-BoxTeam-Workspace-Id` 作为 Gateway 代理层等价载体，并实现「两载体不一致即显式失败」。
- [ ] 3.2 实现「缺失 workspace 身份即显式拒绝」：不复用激活态、不使用默认工作区补齐。
- [ ] 3.3 更新 Gateway：仍负责选目标，但 MUST 显式把目标传给后端；移除「每个工作区一个后端进程」的默认假设，使一个后端进程可服务多个工作区。跨 gateway 时 `gateway authority` 承载稳定 gateway_id，拓扑为 hub-spoke；解析命中只返回稳定身份与内容、不返回 locator，不可解析 fail-closed；上界（visited set / max_transit_gateways=1 / max_gateway_hops=2 / 总 deadline）作为显式策略常量。解析链本体引用「统一虚拟资源寻址」change。
- [ ] 3.4 明确破坏边界并更新对外说明：不带工作区前缀或依赖激活态的既有客户端调用失效。

## 4. 持久化与迁移

- [ ] 4.1 盘点只描述单工作区前提的持久化字段，逐项决定「显式迁移」或「显式失效」，不双读、不留别名。
- [ ] 4.2 实现幂等迁移步骤；迁移遇到旧字段 MUST 显式处理，MUST NOT 静默按旧语义继续解释。
- [ ] 4.3 校验 real path 不变量：迁移后新增/变更的持久化记录与 API 响应体 MUST NOT 出现 real path，只允许 identity + VRN（+ 独立 revision 字段）。
- [ ] 4.4 记录不可回滚点与其理由；确认回滚边界可被明确陈述。

## 5. 契约快照与门禁

- [ ] 5.1 重新生成 OpenAPI 快照（`bun run gen:openapi`）与前端类型，修复 `tests/contracts/api/**` 中因路径形态变化而失效的断言。
- [ ] 5.2 确认门禁在快照未更新时 fail-closed（不会静默通过）。

## 6. 测试影响面（分层口径）

- [ ] 6.1 「只需加 workspace 参数」层：为受影响的服务层/API 单测显式传入 `workspace_id`（优先复用 `tests/unit/core/catalog_workspace_helper.py` 的 `build_catalog_workspace(tmp_path, workspace_id=...)`），不做语义重写。
- [ ] 6.2 「必须重写」层：重写断言单一工作区/直接读 `WORKSPACE_ROOT`/跨工作区复用解析器实例的用例。
- [ ] 6.3 「不受影响」层：确认纯算法/值对象/语法单测无需改动，避免无谓改动。

## 7. 收口在途 change 与命名一致性

- [ ] 7.1 用 `rg` 复核仓库中所有在途 change / 现有 spec 涉及工作区解析或 Gateway 选目标约定者，逐条更新为**指向本 change 的 workspace 身份定义**，消除两套前提并存。
- [ ] 7.2 校验跨 change 命名一致性：逐字使用**冻结契约 v2** 的 `资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`，无同义异名；且本 change 不复述 scope 闭合集与 scope_id 取值语义、不新增拒绝码（以「统一虚拟资源寻址」change 的权威表为准，收到前不定稿）。

## 8. 校验与收尾

- [ ] 8.1 `openspec validate --strict` 对全部受影响 change 零失败。
- [ ] 8.2 确认本 change 未修改任何生产代码（`app/**`、`src/**`、`tests/**`），且未触碰受保护路径。

