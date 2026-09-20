## ADDED Requirements

### Requirement: 联邦配置变化必须由拥有者应用

本地 Gateway SHALL（必须）只监听和应用自己的 Gateway 配置。涉及远程 Gateway 的配置、重启或运行时生命周期变化 MUST（必须）发送给拥有该目标的远程 Gateway 处理；本地 Gateway 只能更新远程工作区投影和展示远程应用结果，不得直接写入远程工作区或绕过远程生命周期。

远程 Workspace 的 pending candidate SHALL（必须）由远程 Workspace 配置域保存和读取。委托链只能传递绑定远程 Workspace/config domain/candidate 的不透明 candidate_ref，并接收远程 generation health proof、promotion 或 recovery 结果；本地 Gateway 不得读取远程或本地 Workspace `.boxteam/` SQLite，也不得把候选 payload 或秘密复制到本地 registry/projection。

#### Scenario: 修改本地 Gateway 配置中的远程目标
- **WHEN** 用户修改本地 Gateway 中远程 Gateway 的连接参数
- **THEN** 本地 Gateway 在本地配置候选中校验并重建该联邦连接，保留其他远程目标不变

#### Scenario: 远程配置要求重启
- **WHEN** 远程 Gateway 报告其配置变化需要重启
- **THEN** 本地 Gateway 展示远程 pending/restart 状态，并把重启操作委托给远程 Gateway

#### Scenario: 远程 Gateway 不可用
- **WHEN** 配置重载期间远程 Gateway 无法连接
- **THEN** 本地 Gateway 保留配置和工作区投影，报告 offline/blocked，不静默切换到其他目标

#### Scenario: 远程 pending 委托
- **WHEN** 远程 Workspace 需要以 pending candidate 重启 backend
- **THEN** 本地只转发不透明 candidate_ref 并等待远程 proof；远程返回的 proof 必须匹配 candidate/revision/digest，本地不能用普通 HTTP healthy 或本地数据库状态代替

### Requirement: 联邦资源状态必须区分投影所有权

本地 registry 中的 `remote_projection` SHALL（必须）保存来源 Gateway 标识、远程目标标识、来源 event cursor 和当前 runtime lease。远程投影不得被本地 `config` 对账删除或改写；只有来源 Gateway 的有序配置/生命周期事件才能改变其投影，事件断档时必须先重新获取快照再对账。

Federation manifest SHALL（必须）返回 `config_event_cursor`、`config_reload_state`、`config_reload_restart_required` 和可选的不透明 `config_reload_candidate_ref`。本地只保存 cursor、状态摘要和委托句柄，不得保存远端候选 payload、secret 或 proof 原文。完整 workspace 快照是断档恢复边界：较旧 cursor 必须拒绝，跳跃 cursor 只有在完整快照成功校验并通过本地 registry CAS 后才能持久化；失败时保留旧 projection、旧 cursor 和 runtime lease。

#### Scenario: 本地配置删除远程投影同名目标
- **WHEN** 本地 Gateway 配置删除一个与远程投影显示名相同的 `config` 目标
- **THEN** 对账依据不可变目标标识和 owner 类型分别处理，不能删除远程投影

#### Scenario: 远程事件 cursor 断档
- **WHEN** 本地 Gateway 收到的远程配置事件 cursor 不连续
- **THEN** 本地暂停该投影的破坏性对账，要求远程快照重同步，期间保留旧投影和租约

#### Scenario: 远程快照游标回退
- **WHEN** 重连后的远程 Gateway 返回小于本地已确认 cursor 的 workspace 快照
- **THEN** 本地拒绝该快照，不替换 projection、runtime lease 或已确认的 cursor

#### Scenario: 远程快照跨越多个事件
- **WHEN** 远程 Gateway 返回大于本地 cursor 的完整 workspace 快照
- **THEN** 本地将其作为断档后的快照重同步，完整 CAS 成功后一次性更新 projection 和 cursor

#### Scenario: 远程隧道仍被使用
- **WHEN** 远程 Gateway 请求删除或替换一个仍有本地代理 lease 的目标
- **THEN** 本地先报告真实 blockers 并完成排空，未完成时保留隧道和投影，不提前关闭连接
