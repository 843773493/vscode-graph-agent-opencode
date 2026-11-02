## MODIFIED Requirements

### Requirement: 名称搜索返回真实定位信息
工作区会话目录搜索 SHALL 匹配文件夹名、会话名、稳定 ID 和逻辑目录路径，并返回 workspace ID、node ID、breadcrumb、相对路径和目录 revision。Gateway 控制面搜索 SHALL 继续匹配工作区名及其导航文件夹名称、ID。名称、ID 与路径查询 SHALL 在去除首尾空白和 Unicode casefold 后支持保持字符顺序的非连续匹配，原有子串查询仍可命中；MUST NOT 先以子串裁掉候选，再仅对剩余首屏进行模糊评分。各字段独立匹配，不得将无关字段拼接成虚假的命中。工作区会话目录分页 SHALL 保留原稳定排序，cursor 绑定规范化查询、匹配语义及目录 revision，查询或 revision 改变时明确失效。Folder 命中只表达导航节点，不建立或启动 Thread。

工作区后端与 Gateway 的在线／离线目录镜像 SHALL 使用相同的节点字段及匹配规则。镜像只消费原 catalog 的公开导出，保留新鲜度与错误状态，不直接读取工作区存储。既有创建日期查询可继续依据冻结 UTC 创建 metadata 匹配，物理 locator 不因此成为公开字段或第二套权威定位关系。

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
