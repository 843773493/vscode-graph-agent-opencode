## MODIFIED Requirements

### Requirement: 配置事件结果必须准确表达整体应用边界

配置候选 SHALL（必须）以整体变更集合为原子应用单位。若候选因需重启、应用失败、冲突或校验失败而未成为 active，则 applied_paths MUST（必须）为空；其中整体进入 pending 的候选 MUST（必须）将全部 changed paths 放入 deferred_paths，不得把可热更新路径误列为已应用。只有 active snapshot 已提交且对应生效策略满足时，路径才能列入 applied_paths。MCP 热应用 MUST（必须）在连接与目录完成受控提交后才报告 applied，并区分后续目录激活边界与已有调用；切换后的退役连接清理失败 MUST（必须）保留已切换事实并单独报告真实清理状态。

#### Scenario: 混合修改整体待重启

- **WHEN** 一个候选同时修改可热更新模型字段和按当前策略仍需重启的字段
- **THEN** 结果为 restart_required，applied_paths=[]，deferred_paths 等于全部 changed paths

#### Scenario: 后续 Job 生效

- **WHEN** 模型字段已提交到 active，但运行中的 Job 保持旧快照
- **THEN** 结果为 applied，该模型路径列入 applied_paths，并通过策略/activation scope 标明从后续 Job 生效，而不是伪装成当前 Job 已改变

#### Scenario: 候选被拒绝

- **WHEN** 候选校验或 CAS 失败
- **THEN** 结果为 rejected 或 conflict，applied_paths=[]、deferred_paths=[]，错误字段包含失败路径和原因

#### Scenario: 状态提交后 relay 崩溃

- **WHEN** SQLite 事务已经提交 active/pending 状态，但 SSE relay 在发布前崩溃
- **THEN** outbox 保留未发布事件，relay 恢复后按 cursor 发布；不得出现状态已变但事件永久丢失

#### Scenario: MCP 热应用成功

- **WHEN** 全部路径可热应用的候选完成 active promotion 和 MCP 受控提交
- **THEN** 对应路径列入 applied_paths，deferred_paths 为空，结果说明目录从允许激活边界生效而非重写旧调用

#### Scenario: 切换完成后清理失败

- **WHEN** 新连接已成为 active，旧连接随后清理失败
- **THEN** 配置事件保留已应用 revision，并报告退役资源的真实失败或待核实状态，不把应用事实倒写成 applied_paths 为空
