# scalable-session-navigation Specification

## Purpose

规定会话目录的分页、搜索、逻辑组织及跨 Gateway 镜像。导航关系、Session／Thread 物理定位和生命周期统一服从原 catalog owner，不从文件系统重建第二套关系。

## Requirements

### Requirement: 会话导航与物理定位必须使用唯一 catalog

工作区后端 SHALL 复用 [itemized-rollout-context](../itemized-rollout-context/spec.md)「产品 Session、durable Thread 与 LangGraph namespace 必须严格分层」规定的唯一 workspace navigation SQLite、稳定 locator、不可变创建 metadata 及 Session／Thread resolver。Folder 是逻辑导航节点，显示名、导航父链和物理目录分别履行原 owner 的职责；本 capability MUST NOT 另建物理 Folder／children 树、manifest 父链或可重建的第二 catalog。

#### Scenario: 在嵌套文件夹创建会话
- **WHEN** 用户在两级会话文件夹下创建会话
- **THEN** catalog 保存逻辑父链，Session 及其 Thread 数据仍由已冻结 locator 解析；文件夹名称和层级不参与物理路径命名

#### Scenario: 删除逻辑文件夹中的会话
- **WHEN** 用户删除某个导航文件夹下的会话
- **THEN** 原删除 owner 协调 catalog 和该 Session 的物理数据，不把导航文件夹解释为可直接删除的磁盘目录

### Requirement: 稳定 ID 解析不得通过扫描吸收手工目录变化

所有会话业务访问 MUST 通过权威 catalog 与统一 resolver 定位。手工移动、重命名、缺失或未知目录 SHALL 沿原 owner 报告明确错误；MUST NOT 扫盘重建父链、吸收未登记 Session 或静默改写 locator。

#### Scenario: 用户手工移动会话目录
- **WHEN** 用户在文件管理器中移动 catalog 已登记的 Session 目录
- **THEN** 后续访问按原 locator 明确失败并保留数据供人工处理，不自动把新路径登记为合法位置

#### Scenario: 发现目录与登记不一致
- **WHEN** 登记 locator 不存在，或维护检查发现未登记／冲突目录
- **THEN** 系统报告具体不一致，不伪造空目录、不覆盖 catalog 或默认吸收磁盘内容

### Requirement: 会话父子关系必须使用逻辑导航父链

会话绑定、解绑与移动 SHALL 使用原 catalog mutation owner；`parent_session_id` 仅按 catalog 最近祖先 Session 派生，不写入 manifest 成为第二事实。导航关系 MUST 与 fork、delegation、上下文来源及运行身份分离；逻辑组织操作不得搬迁 Session／Thread 物理数据。

#### Scenario: 绑定为子会话
- **WHEN** 用户把同一工作区中的会话 B 绑定为会话 A 的导航子会话
- **THEN** 原 owner 提交 B 的逻辑父节点变化，派生父会话为 A；B 及后代的稳定身份、locator 和运行保持

#### Scenario: 解除父会话绑定
- **WHEN** 用户解除 B 与导航父会话 A 的绑定
- **THEN** 原 owner 将 B 移到 A 的逻辑父目录，并重新派生父会话，不移动物理目录或修改上下文来源

#### Scenario: 移动父会话
- **WHEN** 用户把包含后代的会话移动到另一导航文件夹
- **THEN** catalog 事务更新相应父链，后代稳定身份与物理 locator 不变

#### Scenario: 删除父会话及其后代
- **WHEN** 用户明确确认删除包含后代的会话
- **THEN** 原删除 owner 按 catalog 冻结的逻辑子树及各 Session 准入处理；未明确确认的级联删除被拒绝，不以磁盘 children 目录猜测范围

#### Scenario: 拒绝跨工作区绑定
- **WHEN** 用户尝试把另一工作区的会话绑定为当前会话的子会话
- **THEN** 后端明确拒绝，两个工作区的 catalog 与物理数据均不改变

### Requirement: 会话附属数据必须沿原 owner 定位

checkpoint、消息历史、Trace、后台任务、上下文历史、变更、工具结果和生成意图 SHALL 通过稳定 Session／Thread 身份及统一 resolver 访问。附件 SHALL 使用原附件 owner 的资源引用及既有保留／释放合同，不把 Session 路径或临时 inline／data URL 作为另一持久定位关系。

#### Scenario: 会话删除与生成意图清理并发
- **WHEN** 子 Job 已到达终态，同时所属 Session 按原删除协议清理
- **THEN** 已确认生成终态不因其会话包消失而翻转为失败；权限等其它 I/O 错误仍透明报告

### Requirement: 旧布局只能作为显式迁移输入

旧 JSON 导航索引及旧目录布局 SHALL 仅由原 owner 的显式离线迁移处理；支持范围、备份、可恢复提交及校验沿其既有合同。本 capability MUST NOT 恢复启动时的物理树迁移、自动扫盘修复、运行期双读双写或为开发中间态新增兼容链。

#### Scenario: 旧 JSON catalog 尚未迁移
- **WHEN** 工作区只有旧索引而缺少当前 SQLite catalog
- **THEN** 启动明确报告需显式迁移并保留原件，不建立空 catalog 或从磁盘推测父子关系

#### Scenario: 显式迁移中途失败
- **WHEN** 原迁移流程在发布前后失败
- **THEN** 按该 owner 的原提交记录恢复或明确停止，旧输入与未完成目标不会同时成为生产权威

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

### Requirement: catalog 错误必须透明且不由派生镜像替代权威

权威 catalog 损坏、重复身份、非法层级及 locator 不一致 SHALL 沿原 owner 明确失败，维护与备份恢复服从其既有合同。Gateway 镜像和前端范围缓存是派生展示，MUST NOT 用于重建或覆盖权威 catalog。

#### Scenario: catalog 完整性检查失败
- **WHEN** 原 owner 检测到关系或 locator 不一致
- **THEN** 目录 API 保留明确错误并阻止不确定操作，不从磁盘、Gateway 镜像或客户端缓存生成看似成功的空 catalog

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
- **THEN** Web 从剪贴板解析稳定 ID、调用原 catalog 移动 API，并在成功时以返回结果和最新目录 revision 刷新树；失败时重新拉取权威状态并显示错误

### Requirement: Web 树支持受约束的直接拖放组织
Web SHALL 允许拖放工作区、工作区文件夹、会话文件夹和会话，并 MUST 按来源与目标类型映射为单一明确操作。会话和会话文件夹 MUST 仅能在来源工作区内移动；后端仍须独立校验工作区边界、循环关系和逻辑目标类型。

#### Scenario: 拖动会话到文件夹或会话
- **WHEN** 用户把会话拖到同一工作区的会话文件夹或另一会话
- **THEN** 前者将会话逻辑父节点设为目标文件夹，后者设为目标会话；两者均保持 Session／Thread locator 不变

#### Scenario: 拖动会话文件夹到文件夹或会话
- **WHEN** 用户把会话文件夹拖到同一工作区的另一文件夹或会话
- **THEN** 原 catalog 提交文件夹的逻辑父节点，文件夹内节点保留原关系及各自物理 locator，不搬迁目录

#### Scenario: 拖到工作区会话根
- **WHEN** 用户把该工作区内的会话或会话文件夹拖到工作区行
- **THEN** 系统把节点的逻辑父节点改为工作区根并确认最新 catalog 版本，不搬迁物理目录

#### Scenario: 拒绝跨工作区拖放
- **WHEN** 用户把会话或会话文件夹拖到另一工作区中的任意节点
- **THEN** Web 显示不可放置状态，若仍收到请求则后端明确拒绝，来源与目标 catalog 及物理数据均不改变

#### Scenario: 组织变更保留用户展开状态
- **WHEN** 用户折叠工作区后移动或绑定工作区文件夹
- **THEN** Web 保持该工作区折叠，不因导航对象刷新而再次自动定位并展开当前会话祖先

#### Scenario: 叶子会话不显示文件夹空态
- **WHEN** 已展开会话的最后一个子节点被移动出去
- **THEN** Web 将该会话恢复为不可展开的叶子节点，不在会话下显示“空文件夹”；真实空的逻辑文件夹仍显示该空态

#### Scenario: 文件夹创建入口进入节点右键菜单
- **WHEN** 用户要在工作区根或会话的逻辑子节点范围创建会话文件夹
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

