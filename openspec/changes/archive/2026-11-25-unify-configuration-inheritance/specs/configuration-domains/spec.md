## MODIFIED Requirements

### Requirement: 配置按运行时所有权隔离

Gateway 与 Workspace SHALL 各自从明确入口解析本域配置节点及其合法祖先。默认 Gateway 入口为 `${BOXTEAM_HOME}/config/gateway_local.jsonc`；默认 Workspace 入口为当前显式工作区的 `.boxteam/workspace.jsonc`，各默认父链由安装／初始化写明。用户可显式改入口或同域父链，不因固定文件名自动叠加其他来源。Gateway MUST NOT 读取任何工作区 `.boxteam/` 配置或代理解析 Workspace 业务配置；Backend 不读取 Gateway 配置作为默认父。

#### Scenario: 同一 Gateway 管理多个工作区
- **WHEN** Gateway 注册同一台电脑上的多个 Workspace Backend
- **THEN** Gateway 只依据自己的配置图和控制面状态路由，不读取任一工作区的 `.boxteam/workspace.jsonc`

#### Scenario: Workspace 应用本地覆盖
- **WHEN** 工作区入口明确继承用户级 Workspace 链并写入本地 settings
- **THEN** Backend 按实际单父链计算结果，当前工作区覆盖生效，其他工作区不随之改变

#### Scenario: 父链缺失而旧默认文件存在
- **WHEN** 指定入口的父文件不存在，但目录中有原固定层级文件
- **THEN** 启动明确失败，不按旧文件层级猜测替代父

### Requirement: 配置域使用独立 schema 和版本

Gateway 与 Workspace SHALL 各自使用独立 schema 和 config_version。根文档 SHALL 使用统一节点信封；settings 允许共享 network 命名空间和本域业务命名空间，Workspace 还允许 model_options 作为模型共同祖先选项，schema MUST 拒绝本地定义另一域的字段。内嵌节点版本由所属文档管理，不另建每 profile 或整棵树的版本账本。最终 schema 校验 MUST NOT 额外注入不可见默认链。

#### Scenario: Workspace 配置包含 Gateway 字段
- **WHEN** Workspace 来源在 settings 中写入 gateway 或其根文档写入不允许的跨域字段
- **THEN** 配置验证失败并报告来源 URI 与原文字段位置

#### Scenario: Gateway 配置包含 Agent 字段
- **WHEN** Gateway 来源尝试写入 workspace.agents、llm 或其他 Workspace 业务字段
- **THEN** Gateway 配置验证失败，不借通用继承放开业务所有权

#### Scenario: 节点携带另一版本的父文档
- **WHEN** 配置图引用的同域文档版本不是当前可读版本
- **THEN** 明确拒绝并指向适用的原维护入口，不合并不同版本的字段或在普通读取中重写文件

### Requirement: Workspace 配置来源层必须分别物化并支持用户级 fan-out

Workspace SHALL 分别维护实际依赖文件和已有 runtime override 的 payload、layer_revision、layer_digest、规范化来源身份、generation、presence 与 CAS 基线。user、user_local、workspace 不得物化成一个来源；自定义父文档按同规则登记。有效优先级只由真实父链决定，不再由来源类别名称决定。source 更新不等于 active 更新。

共享来源写入或 watcher 导入 SHALL 沿原流程生成 fanout_id；显式 writer 分配关联键，人工编辑由 watcher 按规范化来源路径与写后 digest 派生。该键只用于 fan-out 关联／幂等，不充当 commit revision、event cursor 或内容 digest。实际影响范围 SHALL 由实际依赖该文件的 Workspace 决定，不由 Gateway 管理列表限定；不再依赖该文件的工作区不受本次 fan-out 影响。

存活 Workspace SHALL 自行观察 active／候选图的明确文件依赖并沿原 reload 链独立校验、CAS 和应用。Gateway 重启或工作区登记在另一 Gateway 不改变来源归属，不要求 Gateway 逐个驱动。每个 Workspace 分别分配 candidate_id、commit_revision 和配置事件；未运行的 Workspace 下次启动读取最新父链和来源。不建立跨 Workspace SQLite 全局事务、成员登记或广播机制。

一个 Workspace 冲突 MUST NOT 回滚其他 Workspace 的成功导入。跨 Workspace 诊断仍可汇总 fanout_partial，但它不是单 Workspace 配置事件 result；仅汇总已观察消费者，不承诺完整未知影响范围。被后代覆盖的来源仍更新自身 revision／digest；有效值未变沿原 unchanged 合同处理，来源链改变仍须可见。

API/UI SHALL 明确选择单 Workspace 的原层物化／runtime override，或显式用户／工作区／合法父来源文件。runtime override 为图中可见的 scope 子节点，只写目标 SQLite，不回写父文件；来源文件编辑由原 writer 执行文件 CAS、原子保存和目标物化。其他 Workspace 自行观察；不得隐式改写其他层、父文件或多个 SQLite。文件已保存不代表 active 已生效。

#### Scenario: 用户级 Workspace JSONC fan-out
- **WHEN** 用户修改一次被多个 Workspace 实际继承的 workspace.jsonc，其中有运行和停止的工作区
- **THEN** 运行工作区以同一 fanout_id 独立导入，停止工作区启动时追赶；已自定义父链且不依赖此文件的工作区不受影响，各 commit_revision 互不冒充

#### Scenario: fan-out 中单个 Workspace 冲突
- **WHEN** 一个 Workspace 的 source 已被 API/UI 以更高 revision 修改，共享文件以旧基线到达
- **THEN** 该工作区报告 conflict 并保留 active/pending，其他基线有效的工作区继续，汇总可报告 fanout_partial

#### Scenario: API/UI 指定配置范围
- **WHEN** 用户选择原单工作区 runtime override 或显式来源文件保存
- **THEN** 前者只写目标 SQLite 且继承文件节点，后者沿原文件 CAS writer 保存并导入；不能把一次操作变为全图写回或跨工作区事务

#### Scenario: 明确编辑单工作区来源文件
- **WHEN** 用户绑定精确 Gateway／workspace 和来源文件，用原文件、来源和 active 基线保存局部覆盖
- **THEN** 原 writer 验证实际父链的完整候选并原子保存所选文件，另一工作区不被修改，注释、其他本地定义和秘密引用保持

### Requirement: Active promotion 前必须重新校验全部 source layer 基线

候选开始 apply 时保存的基线只用于准备，不能作为最终 promotion 的充分条件。promotion 前系统 MUST 在原配置域事务中重新校验完整候选依赖及涉及的旧依赖记录，包括 presence、revision、digest、source generation、父边与环境策略代次，以及 active／pending 和适用的 Gateway registry revision。依赖集合不再是固定来源枚举；同一 writer 的并发修改仍受原锁与 CAS 约束。

任一基线变化 MUST 阻止旧候选 promotion；不得只校验改动字段、单一叶子或新图而漏掉已移除父。外部文件读取和 watcher 导入沿原稳定读取／发布边界，不宣称任意外部文件编辑与 SQLite 是跨资源原子事务。最后检查后到达的外部改动 SHALL 按后继 source generation 导入，不反写已封存调用。

#### Scenario: 外部副作用期间 source 发生变化
- **WHEN** 候选 A apply 期间，B 改变任一父来源或将父引用指向另一个文件
- **THEN** A 最终 CAS 失败，不递增 active revision 或发布 applied paths；保留 B desired state，沿原冲突／重建处理

#### Scenario: 最终 CAS 失败前尚未产生副作用
- **WHEN** promotion 发现依赖、active 或 registry 基线不匹配，外部资源尚未切换
- **THEN** 只终止旧 apply claim 并报告 conflict/rebuild_required，不执行破坏性补偿，不改变 active

#### Scenario: 最终 CAS 失败但已有外部副作用
- **WHEN** 父依赖变化时已有 tunnel、process 或消费者完成部分切换
- **THEN** 按原 apply journal 补偿，失败进入 recovery_required，不报告已应用或静默覆盖新候选

### Requirement: 来源正文编辑必须由配置 owner 提供

Gateway 与 Workspace owner SHALL 公布明确目标、source key 和节点位置对应的读写能力，返回脱敏正文、schema、revision／digest、实际父链、有效值及遮蔽关系。未提供 writer 的来源保持只读；既有有限 runtime override PATCH 不扩展为任意文件／路径写入，通用文件保存不替代配置提交。来源 URI 只由原 owner 登记，不由消费者编造。

可写来源 SHALL 提供无副作用预检及文件 CAS writer。预检必须按被修改节点及其实际祖先解析完整候选，包括父引用和被引用 profile 的基线，不要求本地 settings 片段独立满足完整业务 schema。删除覆盖只删除选中节点的定义；编辑／保存保留 JSONC 注释和格式。脱敏占位不得作为原值保存，秘密仍由实际 owner 处理。

#### Scenario: 旧编辑基线与真实文件发生冲突
- **WHEN** 同一来源的另一编辑器或外部文件操作已修改正文，旧草稿提交
- **THEN** 原 writer 拒绝旧文件／source／active 基线，保留草稿并允许重读原目标，不自动重基或覆盖

#### Scenario: 脱敏正文保存与删除覆盖
- **WHEN** 来源含秘密引用和注释，用户只修改普通字段或删除本地覆盖
- **THEN** 原秘密与未改注释保持，删除后显示真实父链结果，不保存遮罩为密码，不修改其他来源

#### Scenario: 只读来源与无副作用预检
- **WHEN** 预检合法草稿，或尝试保存无 writer 的来源
- **THEN** 预检不改变文件、SQLite、active 或运行资源；只读保存明确拒绝，不使用通用文件 API 绕过

#### Scenario: Gateway 来源编辑缺少新连接身份
- **WHEN** 草稿连接未填写 connection_id 且原身份 owner 无法明确匹配
- **THEN** 返回原409合同及位置，文件、SQLite 和运行状态保持，不按数组位置借身份或在预检随机创建身份

#### Scenario: 来源保存需要先完成旧版本迁移
- **WHEN** 提交版本需旧配置转换或连接身份补齐，普通 writer 无法按当前 schema 解释
- **THEN** 拒绝并指向适用的原迁移入口，不在保存后悄悄重写成另一份正文，历史维护入口保持其支持合同

#### Scenario: 修改父引用失败
- **WHEN** 用户将父指向缺失、越权或有循环的节点
- **THEN** 预检拒绝并定位来源链，原 JSONC 草稿可修正，文件和 active 均未改变
