## MODIFIED Requirements

### Requirement: 有效配置优先级

Workspace runtime SHALL（必须）保持用户级 Workspace 配置与显式工作区配置的优先级，并且仅在最终有效配置发生变化时触发重载操作；Gateway 配置不参与该合并。产品诊断日志迁移后 SHALL 从 Gateway `logging.runtime` 获取进程设置，不再作为 Workspace 的配置层。

#### Scenario: 迁移旧 SSH 直连 Gateway 配置
- **WHEN** v3 配置包含 `kind: ssh` 或旧的远程后端直连字段
- **THEN** runtime 将其迁移为 `kind: remote_gateway`、补充 `remote_gateway_port` 并删除新 schema 不再支持的直连字段

#### Scenario: 被遮蔽的低优先级修改
- **WHEN** 低优先级来源修改了仍被工作区配置覆盖的值
- **THEN** 有效 revision 和重载状态保持不变

#### Scenario: 必须重启的配置段变化
- **WHEN** 有效 MCP 或其他被明确分类为需要重启的配置发生变化
- **THEN** 后端报告需要重启，而不是部分应用候选配置

#### Scenario: 仅修改产品日志设置
- **WHEN** 有效 Gateway 候选仅改变日志等级或格式
- **THEN** 候选经真实进程确认后热生效，不进入 Workspace pending_restart

#### Scenario: 日志与需重启的 Gateway 字段一起修改
- **WHEN** 一个 Gateway 候选同时包含日志设置和需要 Gateway 重启的字段
- **THEN** 系统保持候选整体应用规则，不提前热应用日志部分后宣称整个候选已经生效
