## ADDED Requirements

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
目录搜索 SHALL 匹配工作区名、文件夹名、会话名和稳定 ID，并返回 workspace ID、node ID、breadcrumb、相对路径和目录 revision。

#### Scenario: 从搜索结果定位会话
- **WHEN** 用户点击未加载目录中的会话搜索结果
- **THEN** Web 确认最新 breadcrumb、逐级加载祖先并高亮目标会话

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
