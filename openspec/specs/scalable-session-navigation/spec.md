# scalable-session-navigation Specification

## Purpose
TBD - created by archiving change add-scalable-session-resource-generators. Update Purpose after archive.
## Requirements
### Requirement: 物理目录树是会话位置的唯一权威来源
工作区后端 SHALL 将会话本体保存在 `.boxteam/sessions/` 下的真实层级目录中；每个文件夹和会话节点 MUST 通过 manifest 保留稳定 ID，运行时 MUST NOT 再以独立 JSON 关系树作为第二权威来源。

#### Scenario: 在嵌套文件夹创建会话
- **WHEN** 用户在两级会话文件夹下创建会话
- **THEN** 会话的 session manifest、checkpoint、日志、Trace、后台任务、上下文历史、变更和工具结果全部聚合在该物理层级中的同一个会话目录

#### Scenario: 删除日期目录中的会话
- **WHEN** 用户删除某个日期文件夹下的一个会话
- **THEN** 系统只删除该真实会话目录并使派生索引失效，不需要同步维护虚拟文件夹关系

### Requirement: 稳定 ID 解析与手工目录变更可恢复
所有会话业务访问 MUST 通过稳定 ID 解析当前物理路径；扫描 SHALL 从 folder manifest 和 session manifest 重建路径与 breadcrumb，并 MUST 对手工重命名或移动后的合法节点恢复定位。

#### Scenario: 用户手工移动会话目录
- **WHEN** 用户在本地文件管理器中把含合法 session manifest 的会话目录移动到另一合法会话文件夹
- **THEN** 下一次显式刷新或启动扫描保留原 session ID、更新 breadcrumb，并让搜索结果指向新物理路径

#### Scenario: 扫描发现重复稳定 ID
- **WHEN** 两个物理目录声明同一 folder ID 或 session ID
- **THEN** 目录 API 返回包含全部冲突路径的 unhealthy 错误并阻止不确定的写操作

### Requirement: 会话父子关系使用物理子树
物理会话目录 SHALL 通过保留的 `children/` 边界包含子会话和用于组织子会话的真实文件夹；`parent_session_id` MUST 与最近的物理祖先会话一致，目录树是该关系的唯一权威来源。上下文来源 MUST 由独立 `context_source` 表达。

#### Scenario: 绑定为子会话
- **WHEN** 用户把同一工作区中的会话 B 绑定为会话 A 的子会话
- **THEN** 系统把 B 的完整物理子树原子移动到 `A/children/` 下、把 B 的 `parent_session_id` 设为 A，并保留 B 及其后代的稳定 ID

#### Scenario: 解除父会话绑定
- **WHEN** 用户解除 B 与父会话 A 的绑定
- **THEN** 系统把 B 的完整物理子树移动到 A 所在的会话文件夹并清空 B 的 `parent_session_id`

#### Scenario: 移动父会话
- **WHEN** 用户把包含后代的父会话移动到其它会话文件夹
- **THEN** 系统移动父会话目录一次，全部后代随物理子树一起移动且父子稳定 ID 不变

#### Scenario: 删除或导出父会话
- **WHEN** 用户确认删除或导出包含后代的父会话
- **THEN** 删除操作在取得全部后代会话的删除准入后级联删除完整子树，导出操作包含完整子树；未明确确认的级联删除 MUST 被拒绝

#### Scenario: 拒绝跨工作区绑定
- **WHEN** 用户尝试把另一工作区的会话绑定为当前会话的子会话
- **THEN** 后端返回明确错误且两个工作区的物理目录均不改变

#### Scenario: 手工目录变更与 manifest 不一致
- **WHEN** 扫描发现会话的 `parent_session_id` 与最近物理祖先会话不一致
- **THEN** 目录 API 返回包含会话 ID、声明父节点和实际父节点的 unhealthy 错误，不得静默选择其中一个

### Requirement: 会话附属数据使用稳定物理定位
checkpoint、消息历史、附件、Trace、后台任务、上下文历史、变更、工具结果和生成消息意图 MUST 通过稳定 session ID 解析到当前真实会话目录；运行期 MUST NOT 接受 `inline:` 或 data URL 作为持久附件定位器。历史 inline 附件 SHALL 在正常运行前由带锁、带账本的一次性迁移物化进对应会话包并重写引用。

#### Scenario: 迁移历史内联附件
- **WHEN** 旧工作区的消息历史或 checkpoint 引用仅存在于历史 LLM 请求日志中的 inline 媒体
- **THEN** 启动迁移把媒体写入该稳定会话的附件目录、重写引用并记录完成账本，之后运行时只读取稳定附件 locator

#### Scenario: 会话删除与生成意图清理并发
- **WHEN** 子 Job 已到达终态，同时完整会话目录被物理删除
- **THEN** 生成意图视为随会话包清除，已确认的生成终态不得因清理路径消失而翻转为失败；权限等其它 I/O 错误仍必须透明报告

### Requirement: 旧扁平布局一次性可恢复迁移
工作区后端 SHALL 把旧 `.boxteam/sessions/{session_id}/` 与旧逻辑目录关系迁移到物理层级树，并 MUST 使用迁移锁、可恢复账本和迁移后全量校验；迁移成功后旧关系文件 MUST NOT 继续参与运行时读写。

#### Scenario: 迁移已有逻辑文件夹
- **WHEN** 工作区包含旧扁平会话目录和嵌套 `session-folders.json`
- **THEN** 迁移器创建带稳定 ID 的物理文件夹、原子移动对应会话本体、验证全部 ID 后归档旧关系文件

#### Scenario: 迁移已有会话父子关系
- **WHEN** 旧导航或 session manifest 声明同一工作区中的会话 B 以会话 A 为父节点
- **THEN** 迁移器把 B 的完整目录移动到 `A/children/` 下并校验无循环、无缺失父节点和无跨工作区引用

#### Scenario: 迁移中途失败
- **WHEN** 一个目录移动成功后后续移动失败
- **THEN** 下次启动依据迁移账本继续或明确恢复，不得同时把旧 JSON 和部分物理树作为有效权威来源

### Requirement: 会话目录按需分页
工作区后端 SHALL 提供根节点和直接子节点分页接口，并 MUST NOT 要求 Web 在启动时加载该工作区全部会话。

#### Scenario: 展开大型目录
- **WHEN** 一个目录包含超过单页上限的会话和文件夹
- **THEN** Web 只请求首批直接子节点并通过 cursor 加载后续页面

### Requirement: 名称搜索返回真实定位信息
工作区会话目录搜索 SHALL 匹配文件夹名、会话名、稳定 ID 和逻辑目录路径，并返回 workspace ID、node ID、breadcrumb、相对路径和目录 revision。Gateway 控制面搜索 SHALL 继续匹配工作区名及其导航文件夹名称、ID。名称、ID 与路径查询 SHALL 在去除首尾空白和 Unicode casefold 后支持保持字符顺序的非连续匹配，原有子串查询仍可命中；MUST NOT 先以子串裁掉候选，再仅对剩余首屏进行模糊评分。各字段独立匹配，不得将无关字段拼接成虚假的命中。工作区会话目录分页 SHALL 保留原稳定排序，cursor 绑定规范化查询、匹配语义及目录 revision，查询或 revision 改变时明确失效。Folder 命中只表达导航节点，不建立或启动 Thread。

工作区后端与 Gateway 的在线／离线目录镜像 SHALL 使用相同的节点字段及匹配规则。镜像只消费原 catalog 的公开导出，保留新鲜度与错误状态，不直接读取工作区存储。既有创建日期查询可继续依据冻结 UTC 创建 metadata 匹配，物理 locator 不因此成为公开字段或第二套权威定位关系。

原 catalog 游标 owner SHALL 区分 `invalid_catalog_cursor`（编码／载荷、查询或列表作用域、offset 不合法）与 `stale_catalog_cursor`（内容 revision 或 catalog revision 变化）。工作区目录搜索、children／roots 及统一搜索 SHALL 保留既有 HTTP 409，并携带 `owner=session_catalog`、原校验点的 `reason` 和可读原因。Gateway SHALL 沿原字段传递诊断及输入 cursor，失败不交付页或推进续点；离线镜像使用同一校验，不伪造 HTTP 状态，不把错误重新解释为零匹配。

Gateway 跨工作区目录搜索 SHALL 接入统一联邦查询合同，支持明确目标集合或显式当前 Gateway 全部工作区，并在分发前冻结实际范围；单工作区或 A+B MUST NOT 调用全部范围后隐藏其它命中。在线目录匹配 SHALL 交原工作区名称 Provider；已有离线镜像可作为同一合同的派生来源，沿同一领域匹配实现表达更新时间与 stale，不保留 Gateway 独立演化的名称搜索器。工作区名及 Gateway 导航文件夹发现仍由其控制面 owner 处理，不依赖工作区正文或会话镜像。

工作区 catalog 与 Thread 名称查询 SHALL 接受可选的 `X-BoxTeam-Name-Search-Deadline` 有限 UTC 秒时间戳；省略时仍受既有 JSON 请求的十五秒总期限限制，提供时取剩余期限与该上限的较小值，不能借远期时间戳扩大服务端预算。客户端排队、路由和读取共用原期限，Gateway 透传而不续期。期限 SHALL 覆盖冷快照逐行读取、投影、匹配、分页及 Thread 准入等待；过期请求不得开始扫描，耗尽后明确失败，不发布半份快照或伪报完整无匹配。原生阻塞 I/O 与已取得的受保护 worker 仍按原 owner 结算后释放，不宣称可以强行中断系统调用。

#### Scenario: 总期限在进入查询前或扫描途中耗尽
- **WHEN** 经真实 Gateway 的名称查询已经过期，或读取冷 catalog 行与等待 Session 准入期间耗尽同一剩余期限
- **THEN** 返回有 request_id 的明确期限错误，停止后续扫描；事务和准入可继续被正常查询使用，不将未完成结果写入缓存

#### Scenario: 从搜索结果定位会话
- **WHEN** 用户点击未加载目录中的会话搜索结果
- **THEN** Web 确认最新 breadcrumb、逐级加载祖先并高亮目标会话

#### Scenario: 首屏之外的间隔字符缩写
- **WHEN** 用户以间隔字符缩写搜索名称或逻辑目录路径，目标位于目录初始页之外
- **THEN** 原 catalog owner 在分页之前对完整可见候选集执行相同匹配，并以原 cursor 继续结果，不要求客户端先枚举全部会话

#### Scenario: 查询或目录变更使旧搜索游标失效
- **WHEN** 用户将某次查询的 cursor 用于另一查询，或目录重命名、移动及删除已经改变 revision
- **THEN** 搜索明确拒绝旧 cursor，不返回混合页或静默从其它位置继续

#### Scenario: 离线镜像保留相同匹配语义
- **WHEN** 已同步的工作区离线，用户使用同一间隔字符缩写搜索
- **THEN** Gateway 从该工作区原公开导出的目录镜像取得相同归属的候选，并明确标记 stale，不伪报实时查询成功

#### Scenario: 指定集合与显式全部的目录查询
- **WHEN** 调用者分别明确查询 A+B 和当前 Gateway 全部工作区，而登记中还有 C
- **THEN** 前者只调度 A+B，后者先返回包含 C 的实际范围再查询；两者均保持各工作区的原 cursor、覆盖及来源，不由前端隐藏 C 伪装指定范围

#### Scenario: 控制面工作区发现不读取会话正文
- **WHEN** 工具按工作区名称或导航归属发现相关项目，尚未提交领域查询
- **THEN** Gateway 只查询原控制面信息，返回稳定工作区 ID 和 breadcrumb，不导出会话树、扫描正文或启动后端

### Requirement: 目录索引可重建且错误透明
工作区目录索引 MUST 能从物理 folder manifest 和 session manifest 重建；损坏、重复 ID 或非法层级 MUST 使目录报告明确 unhealthy 错误，而不是返回伪造空列表。

#### Scenario: 检测重复会话 ID
- **WHEN** 扫描发现两个目录声明相同 session ID
- **THEN** 目录 API 返回包含冲突路径的错误并阻止不确定操作

### Requirement: Gateway 提供跨工作区轻量搜索
Gateway SHALL 聚合工作区组织、生成器和工作区目录轻量快照，且 MUST 将离线工作区结果标记为 stale/offline。

#### Scenario: 搜索离线远程工作区
- **WHEN** 远程工作区离线但存在上次成功目录快照
- **THEN** 搜索可返回缓存命中并明确标记结果可能过期

### Requirement: Web 树支持大规模展示
Web SHALL 使用懒加载、分页和虚拟化展示目录，并 SHALL 在加载、空结果、离线、失败和完成状态之间提供可信反馈。低频管理操作 SHALL 收纳在节点右键菜单；剪贴板稳定 ID/资源信息 SHALL 可用于移动会话和绑定子会话，不得要求用户手工输入内部 ID。

#### Scenario: 首次打开会话侧栏
- **WHEN** Gateway 管理一百个工作区且每个工作区包含上千个会话
- **THEN** 页面初始化只加载工作区树和可见根节点，不传输全部会话对象

#### Scenario: 使用右键菜单组织会话
- **WHEN** 用户复制会话文件夹信息后在会话上选择移动，或复制会话信息后在另一会话上选择绑定为子会话
- **THEN** Web 从剪贴板解析稳定 ID、调用后端物理移动 API，并在成功时以返回结果和最新目录 revision 刷新树；失败时重新拉取权威状态并显示错误

### Requirement: Web 树支持受约束的直接拖放组织
Web SHALL 允许拖放工作区、工作区文件夹、会话文件夹和会话，并 MUST 按来源与目标类型映射为单一明确操作。会话和会话文件夹 MUST 仅能在来源工作区内移动；后端仍须独立校验工作区边界、循环关系和物理目标类型。

#### Scenario: 拖动会话到文件夹或会话
- **WHEN** 用户把会话拖到同一工作区的会话文件夹或另一会话
- **THEN** 前者把完整会话物理子树移动到目标文件夹，后者把它移动到目标会话 `children/` 下并建立物理父子会话关系

#### Scenario: 拖动会话文件夹到文件夹或会话
- **WHEN** 用户把会话文件夹拖到同一工作区的另一文件夹或会话
- **THEN** 前者形成物理子文件夹，后者把文件夹移动到目标会话的 `children/` 边界下，文件夹内全部节点随目录移动

#### Scenario: 拖到工作区会话根
- **WHEN** 用户把该工作区内的会话或会话文件夹拖到工作区行
- **THEN** 系统把节点移动到该工作区 `.boxteam/sessions/` 根并刷新权威目录树

#### Scenario: 拒绝跨工作区拖放
- **WHEN** 用户把会话或会话文件夹拖到另一工作区中的任意节点
- **THEN** Web 显示不可放置状态，若仍收到请求则后端明确拒绝，来源与目标物理目录均不改变

#### Scenario: 组织变更保留用户展开状态
- **WHEN** 用户折叠工作区后移动或绑定工作区文件夹
- **THEN** Web 保持该工作区折叠，不因导航对象刷新而再次自动定位并展开当前会话祖先

#### Scenario: 叶子会话不显示文件夹空态
- **WHEN** 已展开会话的最后一个子节点被移动出去
- **THEN** Web 将该会话恢复为不可展开的叶子节点，不在会话下显示“空文件夹”；真实空会话文件夹仍显示该空态

#### Scenario: 文件夹创建入口进入节点右键菜单
- **WHEN** 用户要在工作区根或会话的 `children/` 边界创建会话文件夹
- **THEN** Web 在对应工作区或会话右键菜单提供入口，树行右侧不重复显示新建按钮

#### Scenario: 会话资源子窗口统一视觉
- **WHEN** 用户新建、重命名或删除会话资源
- **THEN** Web 使用与工作区重命名窗口一致的暖色主题对话框完成输入和确认，不调用浏览器白色原生 prompt/confirm

### Requirement: 目录缓存必须按权威版本及已加载范围确认有效性

目录同步 SHALL 以稳定 Gateway／工作区身份、catalog_revision、父节点、查询条件及分页范围识别已有数据，区分目录版本与原页／搜索 cursor。前端 SHALL 只缓存已加载目录页；版本变化后失效受影响分支并重新读取，未定义完整目录增量协议时不得直接合并零散事件。变更涉及移动时 SHALL 协调来源与目标分支，避免同时发布混合版本。原 invalid／stale cursor 错误合同 MUST 保留。

#### Scenario: 同版本但请求另一父节点
- **WHEN** 前端已有某目录第一屏，而用户展开相同 catalog_revision 下的另一目录
- **THEN** 系统读取该新范围，不以版本相同返回错误的未变化结论或伪造空列表

#### Scenario: 移动节点与分页并发
- **WHEN** 节点移动期间前端读取来源及目标目录页，得到不同 catalog_revision
- **THEN** 前端保留可信展示并重新确认相关分支，不发布双份节点或把旧页当作新版本续页

### Requirement: 目录操作的确认读取必须传递最低提交版本

目录 committed 回执 SHALL 保留提交版本，后续 children、breadcrumb及所需snapshot／范围确认 SHALL 携带minimum_catalog_revision，各跳 MUST 透传并在相同owner／代际下验证。Gateway 镜像低于此版本时 SHALL 直接读取所需权威范围或明确失败，不得等待全目录镜像落盘作为必需条件。低版本目录页、not-modified或404 MUST NOT 完成本地操作对账或确认节点删除；权威范围达到下限并完成对应投影后才能清理记录。较高版本的自然后续变更属于合法结果，仍须保留原cursor校验。

#### Scenario: 改名成功后旧镜像尚未同步
- **WHEN** 改名已提交版本105而当前Gateway镜像是103，前端进行写后确认
- **THEN** 读取105或更新的权威范围，不以103显示旧名称完成对账；取得权威页无需等待完整镜像同步

#### Scenario: 旧镜像报告父节点不存在
- **WHEN** 新父节点已在版本105创建，旧镜像103对该父节点返回不存在
- **THEN** 系统不能用此旧404移除分支或清理相关操作，必须以不低于105的权威结果确认；目标断连时保持未确认并报告错误

### Requirement: Gateway 目录镜像必须明确来源与同步新鲜度

当前 Gateway SHALL 维护原登记目标的公开目录镜像，并明确 owner、目录版本、镜像更新时间和最近一次权威确认状态。目录浏览能够先返回适用镜像并异步再验证，同步请求 SHALL 合并，不能等待所有目标或把镜像落盘置于全部业务响应之前。前端与镜像版本相同 MUST NOT 被表述为已与远端权威状态一致。目标不可达时保留 stale 及原因；已撤销访问或移除登记后不得继续暴露旧镜像。

#### Scenario: 前端与 Gateway 缓存相同但远端已更新
- **WHEN** 前端和当前 Gateway 均持有版本103，而目标目录已提交版本105
- **THEN** 可以先展示标明来源的103，权威再验证后更新镜像并通知前端重读相关范围，不以103互相相等宣称远端未变化

#### Scenario: 一个远端不可达
- **WHEN** 多目标中一个远端断连而其他目标可用
- **THEN** 可用目标结果独立交付，不可达目标的既有镜像明确标为 stale 并携带同步时间和错误，不阻塞整个目录或伪报该目标为空

### Requirement: 无变化的目录验证不得要求全量传输与重新索引

工作区后端 SHALL 支持以标量目录版本确认是否变化；当前 Gateway 对未变化的目录复用已校验镜像，不重新导出全部节点、全量序列化、重算全目录 hash 或重复落盘。目录版本变化后允许从原公开导出刷新镜像，前端仍按需读取目录页；大目录尚未支持有界快照导出时 MUST 明确其后台成本，不把全量导出加入普通展开请求的阻塞路径。

#### Scenario: 多个页面同时验证未变化目录
- **WHEN** 多个客户端同时读取已有镜像且目标目录版本未变
- **THEN** 当前 Gateway 合并同一 owner 的再验证并复用镜像，前端只获得需要的目录页，不因客户端数量触发对应次数的全目录导出

### Requirement: 目录展示与镜像优化必须验证真实交互终点

目录同步 SHALL 沿现有真实 Gateway 与工作区初始化链路验证，保留分页、虚拟化、精确定位与错误反馈。性能 MUST 包括前端排队、各跳请求、版本确认、必要刷新与渲染；缓存预览不替代确认完成的计时终点。本机回环工作区列表／当前工作区／目录三步导航总 p95≤1000ms、单次≤2000ms，更严格既有预算继续生效；外部网络等待单列。

#### Scenario: 缓存先显示但确认迟迟不结束
- **WHEN** 页面立即画出旧缓存，但目录确认或真实操作就绪超过既有导航预算
- **THEN** 验收记录预览与确认的分别耗时，完整交互仍判超预算，不用首屏占位或缓存可见代替通过

### Requirement: 工作区 Thread 发现由原 owner 有界提供
工作区后端 SHALL 提供 main 与已发布 durable child 的名称／稳定 ID 查询，结果携带工作区、Session、精确 Thread 身份、名称、类型及原寻址 owner 生成的规范 Thread URI。main 身份 SHALL 来自 catalog 冻结指针；child 可见性与名称 SHALL 消费 Session 控制库中原 thread catalog、创建记录及成员冻结标题。查询 MUST NOT 扫描磁盘、加载执行运行态、读取消息正文或要求客户端先拉取全部 Session。Folder MUST NOT 被投影为 Thread。

查询 SHALL 使用相同的顺序字符匹配，在后端限定每次目录扫描、Thread 行数、结果条数及分支调度时间预算，区分完成、结果上限、扫描上限和时间预算。未覆盖部分 SHALL 提供显式续查 cursor，不将预算耗尽报告为全量无匹配。cursor SHALL 绑定 Gateway、工作区、规范化查询、工作区 catalog revision 和当前正在分页的 Session thread catalog revision；范围、查询或这些版本变化 MUST 明确拒绝旧 cursor。跨 Session 控制库的渐进读取不声称是同一时刻的全工作区快照；已完成分支随后发布的新 child 由重新查询获取。

单页一秒分支调度预算耗尽继续沿原未完成状态与 cursor；端到端总期限耗尽则明确拒绝，不给后续 Session 重新分配完整等待时间。

Thread 名称搜索的原游标校验 SHALL 发布 `owner=session_thread_search` 的 `invalid_thread_cursor` 或 `stale_thread_cursor`，由实际校验点区分 `malformed`、`owner_mismatch`、`query_mismatch`、`position_out_of_bounds` 和 `catalog_revision`。当前 Session 的 Thread 发布版本变化 SHALL 由原控制库读取方发布 `owner=session_thread_catalog`、`code=stale_thread_catalog`、`reason=thread_revision`，名称与消息发现共用这一拒绝。原名称入口与统一搜索均保持 HTTP 409、原详细原因及 request_id；Gateway 保留原字段、输入续点及零交付计数，并允许显式重查原分支，不把它们改标为通用读取故障或从头自动执行。

#### Scenario: 未打开的同名 child 与 main 均能发现
- **WHEN** 产品入口在同一 Session 发布一个尚未打开且与 main 同名的 fork child，另一 Session 也有同名 main
- **THEN** 名称查询及续页返回三个不同 owner 的候选，child 保留精确身份，查询不触发模型调用或执行恢复

#### Scenario: 后端重启后继续名称查询
- **WHEN** 首次查询已经返回 cursor，而目录和当前 Session Thread 版本在后端重启前后保持不变
- **THEN** 相同范围和查询可以继续取得剩余候选，不依赖前端已打开标签或进程内搜索缓存

#### Scenario: 预算耗尽与旧游标可区分
- **WHEN** 查询达到扫描或时间预算，或当前正在分页的 Session 新增了 durable child
- **THEN** 前者返回未完成状态、原因及续查 cursor，后者明确拒绝旧 cursor；均不返回虚假的全量无匹配

