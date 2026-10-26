## MODIFIED Requirements

### Requirement: 有效配置优先级

Workspace runtime SHALL（必须）保持用户级 Workspace 配置与显式工作区配置的优先级，并且仅在最终有效配置发生变化时触发重载操作；Gateway 配置不参与该合并。产品诊断日志迁移后 SHALL 从 Gateway `logging.runtime` 获取进程设置，不再作为 Workspace 的配置层。已由连接监督支持的 MCP 变更 SHALL（必须）通过统一候选流水线准备和热应用，不能仅因属于 MCP 段就要求重启；未支持安全替换的路径 SHALL（必须）保留真实重启分类。

#### Scenario: 迁移旧 SSH 直连 Gateway 配置

- **WHEN** v3 配置包含 kind: ssh 或旧的远程后端直连字段
- **THEN** runtime 将其迁移为 kind: remote_gateway、补充 remote_gateway_port 并删除新 schema 不再支持的直连字段

#### Scenario: 被遮蔽的低优先级修改

- **WHEN** 低优先级来源修改了仍被工作区配置覆盖的值
- **THEN** 有效 revision 和重载状态保持不变

#### Scenario: 必须重启的配置段变化

- **WHEN** 按当前生效策略判定需要重启的有效配置发生变化
- **THEN** 后端报告需要重启，而不是部分应用候选配置

#### Scenario: 仅修改产品日志设置

- **WHEN** 有效 Gateway 候选仅改变日志等级或格式
- **THEN** 候选经真实进程确认后热生效，不进入 Workspace pending_restart

#### Scenario: 日志与需重启的 Gateway 字段一起修改

- **WHEN** 一个 Gateway 候选同时包含日志设置和需要 Gateway 重启的字段
- **THEN** 系统保持候选整体应用规则，不提前热应用日志部分后宣称整个候选已经生效

#### Scenario: 受支持 MCP 候选生效

- **WHEN** MCP 候选的全部受影响连接准备成功且完成受控切换和 active 提交
- **THEN** 后端报告实际热应用结果，不创建虚假的待重启状态

### Requirement: 候选配置提交必须原子且可区分待重启状态

系统 SHALL（必须）在配置提交时保持 active snapshot、持久化来源和运行时应用结果的一致性。包含需要重启的配置变化时，系统 MUST（必须）保存已校验的 pending revision，保持旧 active snapshot 继续服务，并明确报告 restart_required；不得把同一候选的一部分应用、另一部分丢弃。全部路径可热应用时 MUST（必须）在所有必要副作用准备成功、受控切换与 active promotion 确认后报告 applied。MCP 目录、连接准入和 active 配置 MUST（必须）具有共同可观察的提交边界；准备失败且旧运行态可保留时报告 apply_failed，无法恢复时报告 recovery_required。

MCP 提交 SHALL 复用现有配置 applying journal、claim 与 promotion owner；candidate_applier 内提前切换、返回后再独立 commit MUST 不被视为共同提交。受控准入 MUST 覆盖 publication 和 active promotion 之间的可观察窗口，恢复 MUST 依据持久阶段核实连接事实，而不是新增配置存储或假定外部 session 可由 SQLite 回滚。

#### Scenario: 候选同时包含可热更新和需重启字段

- **WHEN** 一个候选同时修改可热更新模型配置和按当前策略仍需重启的运行时配置
- **THEN** 系统不应用模型配置的部分变化，保留旧 active snapshot，并报告完整 changed paths 与 pending restart revision

#### Scenario: 重启后应用待重启配置

- **WHEN** 用户完成对应 Gateway 或 Workspace runtime 的受控重启
- **THEN** pending revision 成为新的 active revision，并清除对应待重启状态

#### Scenario: MCP 准备失败

- **WHEN** 全可热应用候选中的某个 MCP Server 初始化失败，而旧连接仍可用
- **THEN** 旧 active 和旧目录保持有效，返回应用失败及具体原因，不部分发布其他已准备 Server

#### Scenario: 连接准备完成但 active 尚未提交

- **WHEN** candidate_applier 或其准备参与方已完成新连接初始化，而 active promotion 尚未确认
- **THEN** preparation、API 和工具调用不能观察候选目录配旧 active 配置；进程在该窗口崩溃后依据现有 journal 和 claim 对账，不宣称候选已应用

#### Scenario: 不可恢复的外部副作用

- **WHEN** 单实例连接替换已停止旧实例，候选失败且旧实例无法恢复
- **THEN** 系统保留候选及实际阶段并报告 recovery_required，不声称数据库回滚已经恢复连接

#### Scenario: 崩溃后在 apply lease 过期前重启

- **WHEN** 进程在 MCP 候选准备后、active promotion 前退出，并在原 apply lease 仍有效时重启
- **THEN** 系统先从已确认 active 重建连接，不提前抢占原 claim；lease 自然到期后主动对账并通过原候选流水线重新验证和提交，不依赖用户再次修改文件，不将原未提交 attempt 改写为成功

#### Scenario: active 已提交但 claim 尚未释放时退出

- **WHEN** active snapshot、pending 终态、apply journal 和配置事件已在同一事务提交，而进程在运行时激活或 claim 释放前退出
- **THEN** 重启依据持久 active 建立新连接，保留原已提交事件及其续读游标，按原期限清理遗留 claim，不重复 promotion、伪造旧 socket 恢复或重放业务工具
