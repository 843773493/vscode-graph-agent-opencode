## MODIFIED Requirements

### Requirement: Gateway registry 资源必须带有所有权和租约

Gateway registry 中的每个目标 SHALL（必须）具有不可变身份、唯一 owner 类型和当前 runtime lease。工作区资源的身份 MUST 是原生 `gateway_id` 与工作区自身 UUID `workspace_id`；连接资源使用 `connection_id`，不得把连接 ID 当成工作区 ID。owner 类型至少包括 `config`、`manual`、`system` 和 `remote_projection`；配置对账只允许创建、更新和移除 `config` 目标，MUST NOT（不得）删除其他 owner 的目标。关闭 SSH 隧道或服务前 MUST（必须）确认没有活动 lease、代理请求或 SSE/WebSocket 使用者，并完成排空。

#### Scenario: 配置删除与手动目标并存
- **WHEN** 新的 Gateway 配置删除一个曾由配置创建的目标，同时 registry 中存在手动目标
- **THEN** 对账只移除对应 `config` 目标，保留 `manual`、`system` 和 `remote_projection` 目标

#### Scenario: 目标仍有活动租约
- **WHEN** 配置更新要求替换一个仍被代理请求或事件流使用的目标
- **THEN** 系统先保留旧 generation 并等待排空；排空失败时保留旧目标和 active revision，不提前关闭隧道

#### Scenario: 远程投影返回删除
- **WHEN** 远程 Gateway 的投影事件声明某个远程工作区已删除
- **THEN** 本地只能按来源 Gateway、工作区 UUID、远程投影 owner 和事件 revision 移除该投影，不得把它当作本地 `config` 目标处理

### Requirement: Gateway workspaces 配置元素必须使用稳定身份

Gateway `workspaces` 配置数组的每个元素 SHALL（必须）包含持久的不可变 `connection_id`，并以该字段作为配置元素及 registry `config` 连接目标的身份。连接发现或注册的工作区 MUST 另以原生 `gateway_id` 和自身 UUID `workspace_id` 定位；不得把 `connection_id` 直接作为工作区身份，也不得派生第二个路由工作区 ID。新 Gateway 配置版本必须将 `connection_id` 纳入 schema；实现 MUST NOT（不得）使用数组下标、host、username、key 或可变连接参数推导身份。缺少该字段的受支持旧配置版本迁移 MUST（必须）在一次显式、可恢复的版本迁移中为每个无歧义元素生成并持久化连接 ID；重复、无法匹配或迁移写入失败必须拒绝，不得每次启动重新生成。

保留同一 `connection_id` 的 host、username 或 key 变化 SHALL（必须）视为同一连接的 generation 更新：新连接健康前保留旧 generation，健康后等待活动 lease、代理请求和 SSE/WebSocket 排空，再转移并关闭旧 generation。工作区身份 MUST 从已确认的原生 Gateway 和 UUID 取得，不能仅因连接 ID 不变而把不同来源工作区当作原资源。只有删除整个配置元素才允许删除对应 `config` 连接目标；`manual`、`system` 和 `remote_projection` 目标不受该对账删除。连接关系移除不得误删经其他有效连接引用的同一工作区。

#### Scenario: 连接参数变化
- **WHEN** 一个 `workspaces` 元素保持 `connection_id` 不变但修改 host、username 或 key
- **THEN** registry 原地更新同一连接的 generation，先建立并确认新 tunnel，再排空旧 tunnel；来源 Gateway 与 UUID 未变的工作区及其持久引用保持不变

#### Scenario: 配置元素删除
- **WHEN** 一个带 `connection_id` 的 `workspaces` 元素从有效配置中删除
- **THEN** 只移除对应 owner=`config` 的连接目标，且在 lease/活动请求未排空前保留旧 generation；其他 owner 的同名或相似目标不受影响，工作区投影继续由其 owner 按有效连接关系处理

#### Scenario: 旧配置迁移与重复身份
- **WHEN** 旧版本数组元素没有 `connection_id`，或迁移后两个元素使用同一 ID
- **THEN** 首次迁移持久化一次性生成的连接 ID；重复 ID 或不确定映射使迁移失败并保留旧配置，不使用数组下标兜底，不改变工作区既有 UUID

### Requirement: Registry reconcile、manual CRUD 和 active promotion 必须使用同一 revision CAS

Gateway registry SHALL（必须）维护单调递增的 registry revision 和可恢复的 apply journal。config batch、manual CRUD、system 更新、remote projection 与 active promotion MUST（必须）声明 expected registry revision，并在同一 SQLite 事务中以 revision CAS 获取写入权；不得通过整表删除重插入绕过 revision 或覆盖其他 owner。registry MUST 按资源职责区分键：工作区资源使用 `(gateway_id, workspace_id)`，连接资源的 `connection_id` 只在明确的 config owner 命名空间中唯一。owner 和 namespace 用于管理授权与对账，不得把同一个工作区变成多个资源身份；对同一工作区的冲突登记必须明确拒绝，不得为不同 owner 各生成别名。

`mutation owner` 与目标的不可变 owner 必须分开校验。system recovery 可以更新已存在目标的运行时状态，但必须保持目标 owner、稳定 identity 和 namespace 不变；system 不得借此创建、删除或改写其他 owner 的配置目标。该更新必须继续遵守 registry revision CAS 并写入 system apply journal。

system default 工作区的生命周期字段仍归 system 所有；为保留用户对默认 Workspace 的导航体验，manual CRUD 只允许修改该工作区的名称和父级组织，且必须保持 owner、工作区 UUID、Gateway 归属、root、backend、managed 和 removable 不变。

#### Scenario: manual CRUD 与 config batch 并发
- **WHEN** config batch 已基于 registry revision N claim，manual CRUD 先提交 revision N+1
- **THEN** config batch 的最终 CAS 失败并报告 conflict，不能删除或覆盖 manual 目标；已产生的外部副作用按 journal 补偿

#### Scenario: config batch 先提交
- **WHEN** manual CRUD 使用旧 registry revision 提交，而 config batch 已提交新的 owner 目标
- **THEN** manual CRUD 被拒绝并返回当前 revision 和冲突目标，不能用旧整表快照覆盖 config batch

#### Scenario: system recovery 更新人工托管目标运行时状态
- **WHEN** Gateway 启动恢复以 mutation owner=system 更新一个已有的 owner=manual 托管工作区的 backend 地址或 connection_error
- **THEN** 更新保持工作区 owner、UUID、Gateway 归属和 namespace 不变，并以 registry revision CAS/journal 提交；system 不能借此删除、创建或改写人工目标的所有权

#### Scenario: owner 名称相同但命名空间不同
- **WHEN** manual、config 和 remote projection 使用相同显示名称或不同来源 Gateway 下相同 UUID
- **THEN** registry 依据稳定资源身份及各自 owner/namespace 执行管理对账，只删除对应 config 资源；名称相同不能导致覆盖，不同 owner 也不能为同一工作区制造不同 UUID
