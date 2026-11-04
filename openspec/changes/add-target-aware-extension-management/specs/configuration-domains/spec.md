## MODIFIED Requirements

### Requirement: Workspace 配置来源层必须分别物化并支持用户级 fan-out

Workspace SHALL（必须）分别维护 `user`、`user_local`、`workspace` 和 runtime override 的 payload、`layer_revision`、`layer_digest`、来源路径和 CAS 基线，不得把 `workspace.jsonc` 与 `workspace_local.jsonc` 合并成一个来源层。有效配置仍按既有优先级合并；source layer 更新不等于 active snapshot 更新。用户级 `workspace.jsonc` 是一个共享 source，写入或 watcher 导入必须生成 `fanout_id`；显式 source writer 直接分配该关联键，直接人工编辑则由 watcher 按规范化 source path 与 post-write layer digest 派生。该键只用于 fan-out 关联/幂等，不得充当 commit revision、event cursor 或内容 digest。

每个 Workspace 必须独立执行 layer/active CAS，并分别分配自己的 candidate_id、commit_revision 和配置事件。运行中的 Workspace 立即尝试导入，未运行的 Workspace 在下次启动时追赶同一 source generation；跨 Workspace 不得伪造一个 SQLite 全局事务。一个 Workspace 冲突不得回滚其他 Workspace 的成功导入，跨 Workspace 汇总结果可报告为 `fanout_partial`，但该名称只属于 fan-out 诊断汇总，不属于单 Workspace 配置事件的 `result` 枚举。被更高优先级层遮蔽的 source layer 仍更新自身 layer revision/digest，但 effective digest 不变并返回 `unchanged`。

#### Scenario: 用户级 Workspace JSONC fan-out
- **WHEN** 用户修改一次 `workspace.jsonc`，且多个 Workspace 中有的正在运行、有的已停止
- **THEN** 每个 Workspace 按同一 `fanout_id` 独立导入并产生自己的结果；运行中的立即处理，停止的在启动时追赶，任何一个 Workspace 的 commit_revision 都不冒充其他 Workspace 的 revision

#### Scenario: fan-out 中单个 Workspace 冲突
- **WHEN** 一个 Workspace 的 `user` layer 已被 API/UI 以更高 revision 修改，随后共享 `workspace.jsonc` 以旧基线到达
- **THEN** 该 Workspace 进入 `conflict` 并保留自己的 active/pending，其他基于各自有效基线的 Workspace 可继续导入；汇总诊断报告 `fanout_partial` 和逐 Workspace 结果

#### Scenario: API/UI 指定配置范围
- **WHEN** API/UI 修改 Workspace 配置
- **THEN** 请求必须明确是单 Workspace 的 `workspace`/`user_local`/runtime override，或是用户级 source；原单 Workspace 层物化／runtime override 请求只写目标 SQLite；显式来源文件编辑由原 source writer 先执行文件 CAS／原子保存，再沿原导入和候选链物化目标层；用户级 source 请求先原子写 JSONC 再触发 fan-out。入口不得隐式改写其它层或多个 SQLite，文件已保存不代表 active 已生效

#### Scenario: 明确编辑单工作区来源文件
- **WHEN** 用户选择指定 Gateway／workspace 的 workspace 来源文件，以文件及层／active 基线保存一个覆盖片段
- **THEN** 原 source writer 验证合并后的最终配置并原子保存所选文件，再沿原导入／候选链更新该工作区；runtime override API 不回写文件，另一工作区不被修改


## ADDED Requirements

### Requirement: 来源正文编辑必须由配置 owner 提供

Gateway 与 Workspace 配置 owner SHALL 公布各来源层的读取与写入能力，返回绑定明确目标和 source key 的脱敏正文、schema、来源 revision／digest、有效值及遮蔽关系。未提供 writer 的层必须只读；现有有限 runtime override PATCH 不扩展为任意 JSON 或路径写入，通用文件保存不替代配置提交。来源资源未登记 URI 时保持无 URI，消费者不得编造定位。

可写来源 SHALL 提供无副作用预检及文件 CAS writer；预检按实际来源优先级合并后执行 schema／业务校验，不要求覆盖片段具有完整配置。删除覆盖只移除选中层的键，不能把下层默认值复制回该层。编辑与保存保留 JSONC 注释及格式，秘密仅在实际 owner 处理；正文中的脱敏占位不得被当作原值保存。

#### Scenario: 旧编辑基线与真实文件发生冲突
- **WHEN** 两个编辑器读取相同来源，另一编辑器或原生文件操作已改动来源后，旧编辑器提交
- **THEN** writer 明确拒绝旧文件／层／active 基线，不覆盖新内容，并允许重新读取原目标来源处理冲突

#### Scenario: 脱敏正文保存与删除覆盖
- **WHEN** 用户读取含秘密及注释的来源，仅修改普通字段或删除其中一个覆盖键
- **THEN** 原秘密及未修改注释保持，选定键删除后显示真实下层值，不保存遮罩为密码，不修改其它层

#### Scenario: 只读来源与无副作用预检
- **WHEN** 用户预检可写来源的合法修改，或尝试保存未提供 writer 的来源层
- **THEN** 预检不改变文件、SQLite、active 或运行资源；只读层保存明确拒绝，不使用通用文件 API 绕过
