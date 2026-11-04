## MODIFIED Requirements

### Requirement: 联邦工作区投影

本地 Gateway SHALL（必须）以来源原生 Gateway 的稳定 `gateway_id` 和工作区自身 UUID `workspace_id` 表示远程工作区，并 SHALL（必须）通过远程 Gateway 路由业务请求。投影 MUST 保留来源 UUID，不得生成本地 `gw_...` 工作区 ID 或维护 `remote_workspace_id` 换算。`connection_id` 只标识连接；同一来源工作区经不同连接导入时不得改变其资源身份，不同 Gateway 暴露相同 UUID 时不得合并业务状态。

#### Scenario: 请求远程工作区
- **WHEN** 当前活动工作区属于远程 Gateway
- **THEN** 本地 Gateway 携带同一个工作区 UUID 和明确目标 Gateway 归属向该 Gateway 转发请求，并保留原 request ID

#### Scenario: 相同 UUID 来自不同 Gateway
- **WHEN** 发现两个来源 Gateway 各自直接管理相同 UUID 的工作区
- **THEN** 投影、活动选择及代理按来源 Gateway 区分两者，不生成本地工作区别名

### Requirement: 联邦工作区可参与本地目录与生成器配置

本地 Gateway SHALL 允许工作区文件夹、目录搜索和 GeneratorDefinition 引用其直接导入的远程工作区投影，并 MUST 使用来源 `gateway_id` 与原工作区 UUID 路由到远程 Gateway。持久引用不得保存本地投影别名或以 `connection_id` 代替工作区身份；连接更新后只要来源归属不变，原引用必须继续有效。

#### Scenario: 生成器挂载远程会话
- **WHEN** 用户把生成器挂载到直接导入的远程工作区会话
- **THEN** 本地 Gateway 通过远程 Gateway 代理校验和执行，并在远程离线时保留配置且标记 blocked/offline

#### Scenario: 远程连接参数更新
- **WHEN** 同一来源 Gateway 的连接完成受控 generation 更新
- **THEN** 已有目录和生成器引用继续使用原 Gateway 归属与工作区 UUID，无需改写成新工作区 ID

### Requirement: 联邦资源状态必须区分投影所有权

本地 registry 中的 `remote_projection` SHALL（必须）保存来源 Gateway 标识、工作区自身 UUID、连接关联、来源 event cursor 和当前 runtime lease。工作区身份 MUST 是 `(gateway_id, workspace_id)`，owner 类型和连接 ID 不得成为第二套工作区定位关系。远程投影不得被本地 `config` 对账删除或改写；只有来源 Gateway 的有序配置/生命周期事件才能改变其投影，事件断档时必须先重新获取快照再对账。

Federation manifest SHALL（必须）返回 `config_event_cursor`、`config_reload_state`、`config_reload_restart_required` 和可选的不透明 `config_reload_candidate_ref`。本地只保存 cursor、状态摘要和委托句柄，不得保存远端候选 payload、secret 或 proof 原文。完整 workspace 快照是断档恢复边界：较旧 cursor 必须拒绝，跳跃 cursor 只有在完整快照成功校验并通过本地 registry CAS 后才能持久化；失败时保留旧 projection、旧 cursor 和 runtime lease。

#### Scenario: 本地配置删除远程投影同名目标
- **WHEN** 本地 Gateway 配置删除一个与远程投影显示名相同的 `config` 目标
- **THEN** 对账依据工作区归属、连接关系和 owner 类型分别处理，不能删除远程投影

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
