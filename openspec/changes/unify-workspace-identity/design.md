## Context

动机见 `proposal.md`。Backend 已有工作区 UUID owner：`app/core/workspace_identity.py` 管理工作区内的身份文件，挂载和资源 URI 使用该 UUID。Gateway 的 `workspace_ids.py` 另生成 `gw_...`，registry 用它作为目标键，再用 `backend_workspace_id` 或 `remote_workspace_id` 完成代理转换。联邦还会按连接生成本地投影 ID，客户端因此既保存路由 ID 又维护业务 owner 转换。

当前多工作区 Backend 已有唯一 managed owner、完整组生命周期、唯一原生 Gateway root binding 和工作区内独占锁。本设计沿用这些能力，不回退为一个工作区一个进程。此前 `add-multi-workspace-backend-mounting` 中配对 target ID 与 Backend UUID 的条文由本变更替代，其余身份载体、管理授权及生命周期合同继续有效；原变更完成记录不改为未完成。本变更规定唯一 UUID 值与跨域迁移，不另建身份初始化或 URI grammar owner。

## Goals / Non-Goals

**Goals:**

- 从身份源到 registry、代理、协议与客户端恢复只传同一个 UUID，移除实际换算及全部调用方。
- 在不改变业务历史和物理存储布局的前提下，完成控制面引用的一次性迁移。
- 让连接重建和进程 generation 变化只影响运行时，不影响工作区资源身份。

**Non-Goals:**

- 不设计跨机器 UUID 总注册表、自动故障切换或多 Backend 同时写同一工作区。
- 不新增永久 home 绑定、资源 ACL、第二套管理权限或占用账本。
- 不改写 canonical 历史、历史请求 hash、Session／Thread ID，不强制更换 UUID 版本。
- 不在正常启动中隐式迁移实际库；存量迁移沿下述显式维护流程执行。

## Decisions

### 1. 复用既有工作区身份源

`.boxteam/workspace-identity.json` 中的标准 UUID 为唯一值。已有工作区只读取并校验；新工作区沿既有授权注册与身份初始化链路创建。Gateway 不直接遍历或读取工作区业务目录，而通过已有目标侧 bootstrap／身份读取入口获得 UUID，再发布 root binding 和工作区登记。只读发现不得顺便创建身份。

注册前只有原生根或连接参数时，使用现有注册操作状态及连接 ID，不占用工作区资源键。登记冲突按原生 Gateway 内 UUID 与根的唯一关系报错；受控根替换继续由原 owner 授权，不将替换操作变成重新分配 UUID。磁盘复制带来的重复 UUID 不在此次变更中自动修复。

不采用路径哈希、URL 哈希、将 `gw_...` 改名为 `target_id` 或另建全局 UUID 表：这些方案仍会维持同一工作区的双重权威身份。

### 2. 工作区 owner 与连接、进程分别建模

| 字段或关系 | 职责 | 本次处理 |
| --- | --- | --- |
| `workspace_id` | 工作区自身 UUID | 各层统一为既有值 |
| `gateway_id` | 原生目标 Gateway 归属 | 跨 Gateway 引用显式保存 |
| `connection_id` | 配置连接／SSH 隧道等连接元素 | 保留，不参与生成工作区 ID |
| `owner_id`、`generation`、lease／fencing | Backend 或连接运行时管理 | 保留独立职责 |
| `backend_workspace_id`、`remote_workspace_id`、投影 `gw_...` | 同一工作区的重复身份或映射 | 迁齐消费者后物理删除 |

registry 的工作区键为 `(gateway_id, workspace_id)`；本地工作区可由 registry 的原生 Gateway 归属提供固定 gateway 分量，但持久导出和跨 Gateway 引用不能丢该分量。管理 owner 与 namespace 仍决定谁能改写记录，不再构成可制造另一工作区身份的键。

同一远程工作区经多连接出现时共享一个工作区记录，连接关联独立保留。删除某条连接只解绑该关系；其余有效关系不能被误删。不新增自动切换策略；现有规则无法唯一选择可用连接时报告明确状态，不随机选一条。两个原生 Gateway 声明相同 UUID 时按 Gateway 归属隔离，不推断二者共享业务状态。

### 3. 显式路由传递同一个 UUID

保留现有 `/api/v1/workspaces/{workspace_id}/...` 和等价 `X-BoxTeam-Workspace-Id`；二者有值时必须一致。Gateway 面向客户端的跨 Gateway 工作区请求以 `X-BoxTeam-Target-Gateway-Id` 表达目标归属，资源 URI 则沿其既有 gateway authority 解析。Gateway 自有 DTO 选择工作区时使用同样的结构化 owner，不用拼成另一个字符串工作区 ID。

代理剥离不可信的发送方身份头，继续由每跳 Gateway 注入自身 `X-BoxTeam-Gateway-Id` 并使用原凭据认证。目标选择头不构成授权；到原生 Gateway 时验证其与自身归属及已登记工作区一致，再以原 UUID 代理 Backend。中继只解析现有直连来源，不扩大有界联邦拓扑。一个纯本机入口的目标 Gateway 由请求连接的原生 Gateway 上下文确定；跨 Gateway 恢复与请求必须携带明确归属，不能借当前活动工作区补齐。

协议 descriptor／proto、公开 DTO、生成映射与校验、SDK、错误和响应头同步更新。破坏性协议变化依既有版本协商拒绝不兼容 peer，不能通过“对新端发送 UUID、对旧端发送别名”保留双轨。

### 4. 共享 Backend 不再保存成员双身份

`ManagedBackendMember` 和其启动合同只保存成员 UUID，并由 managed owner 提供所属原生 Gateway、generation。完整影响集合与 blocker 一律用同一个 UUID，远程委托附 Gateway 归属。root binding 的唯一持久来源仍在原生 Gateway registry；Backend mount 表只持有启动得到的内存视图。

生命周期迁移沿既有完整组流程：重启、force、移除、shutdown、preserve/adopt、恢复及配置 fan-out 全量迁齐；不重做状态机。控制面记录更新不能绕过 registry revision CAS，不迁移进行中的 lease 为“已排空”。

正式 Backend 通过工作区内 `state/workspace.sqlite` 相邻的现有进程锁取得独占。锁跟随物理根，切换 `BOXTEAM_HOME` 不改变占用结果。锁释放后可合法接管，不增加永久 owner 标记。附件维护用途的 shared lock 不等同于正式 Backend 的独占保障。

### 5. 一次性迁移控制面引用，不重写业务正文

先盘点受支持旧版本及真实持久字段；使用旧登记中已有 UUID 与可信目标侧身份核验生成迁移映射。映射必须包含登记所在 Gateway、旧别名和来源 Gateway／UUID，不能仅以远端旧 ID 或显示名为键。该映射只属于显式迁移材料，不参与普通请求解析。

迁移覆盖 registry 主外键与 metadata、root binding、`managed_backends` 成员、活动选择、导航／Generator／user_view_state 和客户端 serializer／缓存。共享 Backend 的根数据只更新唯一 binding，不复制进成员合同。客户端可通过同一显式迁移结果导入恢复引用；无法确认的旧项明确报告，不能重新打开到当前工作区。迁移材料不在普通 workspace DTO 中长期暴露。

服务停止后运行 `migrate-workspace-identity`，沿已有备份、CAS 与启动屏障发布；远端通过 `--verify-remote` 先核验，`--restore` 只恢复同批原件。发布完成后才导出客户端恢复材料，`migration_id` 为标准 UUID。浏览器共享数据库可能包含多个登记 Gateway 的旧分区，显式导入入口允许一次选择多份材料，并按原登记来源在同一队列事务中转换；缺少来源材料时保留整批及高水位，不先升版或派发。恢复材料仅在维护操作中使用，不成为普通请求的别名表。

多连接合并为同一资源后，若旧导航节点或用户视图发生冲突，不任意选择一个值覆盖其它记录。导航重复 owner 沿现有唯一节点校验明确阻塞并保留原件；用户视图仅在完整记录等价时去重，否则要求先解决冲突。

旧 Generator 的退休是升级前置步骤：仍引用旧工作区别名的源文件及未结算冻结快照，必须在能够读取原 registry 13 的升级前版本中，通过原显式导入入口完成移交和退休，再停止服务并执行身份切换。新版本不会为此恢复旧 registry 运行链，也不能在缺少原权威映射时推测别名归属；预检继续保留原件并明确阻塞。已经使用 canonical owner 的旧格式文件仍沿当前显式入口处理，已结算的原备份、回执和 source_hash 原样保留。

有效资源 URI 和 canonical owner 已经使用正确 UUID 的记录保持原值；不全库字符串替换、不重写历史请求与 hash。仅实际改变 SQLite 结构时提升该库 schema 版本；纯值与引用迁移使用明确的数据迁移版本，不把 SQL 文件抽离当作前置。

替代方案是运行时维护旧别名查询或清空所有客户端恢复状态。前者继续双轨，后者丢失用户状态，因此均不采用。

### 6. 以真实注册与恢复入口验收

验收先由产品管理入口创建独立工作区和共享 Backend，再确认公开 DTO、代理请求、URI、目录与 Generator、编辑器持久恢复使用相同 UUID。迁移测试使用受支持旧版本的完整控制面输入，校验引用关系及重入结果，不手工补齐迁移后的关联来代替入口。

补充两个 Gateway 同 UUID、同原生 Gateway 不同根冲突、多连接同来源、远端离线和协议不兼容场景。正式独占回归须在独立子进程里更换 home 争用同一隔离工作区，并验证锁释放后接管；不能只验证锁 helper。保留现有多工作区隔离、组生命周期及授权拒绝场景。

## Risks / Trade-offs

- [既有 `dict[str, ...]`、缓存与路由只按工作区 ID 分区] → 盘点间接消费者，跨 Gateway 的键统一保留结构化 owner，不把复合键序列化成另一 workspace_id。
- [配置 owner 以连接作为 target 身份，直接替换易误删投影] → 分离连接记录与工作区关系，保留 owner、租约、cursor 和 CAS 行为；同资源冲突拒绝发布。
- [旧远端离线，映射无法核验] → 迁移报告阻塞，保留原状态供显式重试，不按本机路径猜测或半迁移启动。
- [浏览器存储与控制面数据库不能跨系统原子提交] → 先生成校验完整的恢复材料并原子发布服务端迁移，客户端逐个存储批次升级；失败项可重试且不被静默清空。
- [另一个功能继续新增旧别名消费者] → 实施前再次全仓检索，quick-access 与扩展管理采用本合同，迁齐后删除旧生成器和公开字段。

## Migration Plan

### 已核对的输入与引用范围

迁移输入基线为当前 registry 合同 13、Gateway SQLite schema 18、Gateway 配置格式 4 及 `gateway_connection_ids` 记录格式 4。低于 registry 13 的输入已被现有生产入口拒绝，不在此次新增兼容范围；其它旧配置先走其既有显式升级。SQLite 结构版本、registry payload 合同和配置 revision 各有原 owner，不能混为一个版本号。`workspaces.json` 合同 13 仅在没有权威 SQLite 登记时作为一次性迁移输入；两处并存且冲突时不得静默择一。

| 链路／持久输入 | 已确认的旧引用与处理边界 |
| --- | --- |
| 本地登记 | `workspace_ids.py` 的路径／URL 哈希由 `managed_workspaces.py`、`server/bootstrap.py`、`routes/workspaces_lifecycle.py` 消费；替换取得 UUID 的入口，保留管理授权、root binding 与 CAS，不仅重命名生成器。 |
| 共享 Backend | `ManagedBackendMember`、`WorkspaceRuntime`、registry runtime、controller／group_actions、health proof 和 startup recovery 同时携带别名与 UUID；完整成员迁齐，`owner_id`、generation、lease、fencing 不参与身份替换。 |
| 联邦与连接 | `build_projected_workspace_id` 在 `federation/__init__.py`，由 `remote_gateway.py` 发布；projection／config batch、remote lifecycle、辅助代理及 skill source 消费。保留连接身份与每条连接的 cursor／租约，工作区按原生 Gateway＋UUID 合并。 |
| 请求与公开协议 | `server/workspace_proxy.py`、resource navigation／text mutation、auxiliary proxy、catalog/resource catalog、workspace port，以及 proto 的 manifest／remote summary／workspace DTO 都有换算；清除旧 Backend 身份头与两套 ID 字段，同步生成产物和 SDK，不保留别名协议分支。 |
| Gateway SQLite | `gateway_workspace_registry` 的主键／payload／active 标记、`workspace_registry_meta` 的活动项／managed_backends，以及 `user_view_state` 主键中的旧工作区引用必须带准确归属迁移；保留 registry revision 和 apply journal，不重放已结算的历史 journal。未结算 config／restart／generation 状态先阻塞并排空，不能按新格式假称已完成。 |
| Gateway 文件 | `navigation/workspace-tree.json` 格式 1 按字段迁移，保留导航 `node_id`／父链。旧 `generators/*.json` 和 `migration-backups/session-generators/` 的冻结导入快照绑定原来源 bytes、映射与 `source_hash`：先由既有显式导入入口完成并退休运行源；未结算导入阻塞身份切换，已完成备份与回执保留，不修改原文再伪称原 hash。catalog 格式 3 已按真实 `(gateway_id, workspace_id)` 保存的快照不改写。 |
| Web 持久引用 | `boxteam-browser-views` IndexedDB v1 的 selection、session view、settings 中折叠项及草稿键可能含别名；`boxteam-workspace-file-intents` v1 同时有 `gatewayWorkspaceId`／`backendWorkspaceId`；`boxteam-session-catalog-outbox` v2 的分区已有 Backend UUID，但 Gateway 分量需核确切来源。保留命令 ID、顺序、高水位及未知结果，不清空队列；旧分区不足以唯一确定来源时保留并阻塞派发。 |
| Workbench 恢复 | Thread serializer v2 已保存真实 Gateway／workspace／Session／Thread，readonly serializer v1、搜索范围 v1 和合法资源 URI 同样保留既有正确 owner；临时 `routeId` 只在重连时重新解析。旧未版本化 Thread owner 缺少来源时继续保留不可用记录，不能借当前活动项补齐。 |

纯内存的 HTTP target、hook dependency、目录镜像错误键、请求 barrier、文件操作、资源浏览和 MCP／持久任务消费者也须一并切换结构化 owner。`gatewayWorkspaceId`／`backendWorkspaceId` 的 camelCase 调用链和 `X-BoxTeam-Backend-Workspace-Id` 属于检索范围；不能只搜索 DTO 的 snake_case 字段。Agent／Session 服务中只是把实际 UUID 参数命名为 backend 的位置，按字段职责统一命名，不把其 canonical 历史正文作为别名记录重写。

`add-workbench-quick-access` 与 `add-target-aware-extension-management` 后续消费同一结构化 owner；不会为了新查询或扩展宿主保留另一套工作区路由身份。升级前必须保留上述各客户端格式的完整迁移测试输入；正确的 URI／serializer 不因其它格式升级而无意义换版本。

1. 明确支持的 registry／配置／客户端旧版本，冻结受影响管理写入并排空使用者，按现有方式停止相关 Gateway／Backend；不新建长期迁移服务。
2. 只读预览并校验身份、完整引用、owner 与 generation；远端离线、重复 UUID 或未结算配置候选等阻塞必须先解决。保留旧控制面备份及客户端恢复材料，不备份或公开凭据到迁移结果。
3. 在现有 SQLite 事务边界内迁移所有控制面引用和必要 schema，提交前完成引用完整性验证。跨文件步骤通过现有迁移协调保证可重入，未完成不得公布成功或开始正常服务。
4. 同一发布切换服务端与客户端协议；由恢复材料升级旧编辑器、活动选择和视图态，保留逐项错误。删除运行时别名生成、换算和旧字段，不开放旧协议流量。
5. 经真实产品入口验证注册、组重启、联邦、持久恢复和独占。失败时在尚无新业务写入的维护窗口恢复同批控制面备份和旧版本；如已有新写入，停止服务并显式修复，不把旧备份覆盖到新状态上。
