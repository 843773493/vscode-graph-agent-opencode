## MODIFIED Requirements

### Requirement: 名称搜索返回真实定位信息
工作区会话目录搜索 SHALL 匹配文件夹名、会话名、稳定 ID 和逻辑目录路径，并返回 workspace ID、node ID、breadcrumb、相对路径和目录 revision。Gateway 控制面搜索 SHALL 继续匹配工作区名及其导航文件夹名称、ID。名称、ID 与路径查询 SHALL 在去除首尾空白和 Unicode casefold 后支持保持字符顺序的非连续匹配，原有子串查询仍可命中；MUST NOT 先以子串裁掉候选，再仅对剩余首屏进行模糊评分。各字段独立匹配，不得将无关字段拼接成虚假的命中。工作区会话目录分页 SHALL 保留原稳定排序，cursor 绑定规范化查询、匹配语义及目录 revision，查询或 revision 改变时明确失效。Folder 命中只表达导航节点，不建立或启动 Thread。

工作区后端与 Gateway 的在线／离线目录镜像 SHALL 使用相同的节点字段及匹配规则。镜像只消费原 catalog 的公开导出，保留新鲜度与错误状态，不直接读取工作区存储。既有创建日期查询可继续依据冻结 UTC 创建 metadata 匹配，物理 locator 不因此成为公开字段或第二套权威定位关系。

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

## ADDED Requirements

### Requirement: 工作区 Thread 发现由原 owner 有界提供
工作区后端 SHALL 提供 main 与已发布 durable child 的名称／稳定 ID 查询，结果携带工作区、Session、精确 Thread 身份、名称、类型及原寻址 owner 生成的规范 Thread URI。main 身份 SHALL 来自 catalog 冻结指针；child 可见性与名称 SHALL 消费 Session 控制库中原 thread catalog、创建记录及成员冻结标题。查询 MUST NOT 扫描磁盘、加载执行运行态、读取消息正文或要求客户端先拉取全部 Session。Folder MUST NOT 被投影为 Thread。

查询 SHALL 使用相同的顺序字符匹配，在后端限定每次目录扫描、Thread 行数、结果条数及分支调度时间预算，区分完成、结果上限、扫描上限和时间预算。未覆盖部分 SHALL 提供显式续查 cursor，不将预算耗尽报告为全量无匹配。cursor SHALL 绑定 Gateway、工作区、规范化查询、工作区 catalog revision 和当前正在分页的 Session thread catalog revision；范围、查询或这些版本变化 MUST 明确拒绝旧 cursor。跨 Session 控制库的渐进读取不声称是同一时刻的全工作区快照；已完成分支随后发布的新 child 由重新查询获取。

单页一秒分支调度预算耗尽继续沿原未完成状态与 cursor；端到端总期限耗尽则明确拒绝，不给后续 Session 重新分配完整等待时间。

#### Scenario: 未打开的同名 child 与 main 均能发现
- **WHEN** 产品入口在同一 Session 发布一个尚未打开且与 main 同名的 fork child，另一 Session 也有同名 main
- **THEN** 名称查询及续页返回三个不同 owner 的候选，child 保留精确身份，查询不触发模型调用或执行恢复

#### Scenario: 后端重启后继续名称查询
- **WHEN** 首次查询已经返回 cursor，而目录和当前 Session Thread 版本在后端重启前后保持不变
- **THEN** 相同范围和查询可以继续取得剩余候选，不依赖前端已打开标签或进程内搜索缓存

#### Scenario: 预算耗尽与旧游标可区分
- **WHEN** 查询达到扫描或时间预算，或当前正在分页的 Session 新增了 durable child
- **THEN** 前者返回未完成状态、原因及续查 cursor，后者明确拒绝旧 cursor；均不返回虚假的全量无匹配
