## MODIFIED Requirements

### Requirement: Workspace 配置按生效范围分类

Workspace SHALL（必须）为有效配置路径声明可观察的生效范围。模型、Agent 和开发运行参数的变化 MUST（必须）至少对后续 Job 生效；默认 Agent 的变化 MUST（必须）只影响后续新会话；UI 和按请求读取的配置变化 MUST（必须）对后续读取生效；当前 Job、当前会话的既有绑定和已经建立的请求 MUST（必须）保持原语义。产品诊断日志 SHALL 属于 Gateway 的 `logging.runtime`，不再由 Workspace logger 决定，日志等级与格式不属于 Workspace 必须重启的配置。

#### Scenario: 修改模型或 Agent 配置
- **WHEN** 有效 `llm` 或 `agents` 配置热重载成功
- **THEN** 后续 Job 使用新配置，正在运行的 Job 不被替换或重建

#### Scenario: 修改默认 Agent
- **WHEN** 有效 `default_agent` 发生变化
- **THEN** 新建会话使用新的默认 Agent，已有会话继续使用其已持久化的 Agent 标识

#### Scenario: 修改已被启动时缓存的运行时依赖
- **WHEN** 修改 MCP 或其他经配置策略判定为不能安全热替换的运行时依赖
- **THEN** 系统报告 Workspace backend restart required，并不得声称该配置已经即时生效

#### Scenario: 修改产品日志等级
- **WHEN** 用户修改 Gateway 有效 `logging.runtime.level`
- **THEN** 系统走产品进程日志热应用，不要求重启 Workspace，也不改变工作区业务配置的合并顺序

## ADDED Requirements

### Requirement: 产品诊断日志配置归 Gateway 控制面

Gateway 配置 SHALL 提供 `logging.runtime.level` 与 `logging.runtime.pretty`，默认 `info` 与 `true`，只接受有效等级和严格布尔值；配置 SHALL 覆盖本机受管产品进程。Workspace schema MUST 拒绝 logger 或 Gateway logging 字段。两个配置域 MUST 分别升级配置版本，并通过显式可恢复迁移迁出旧 logger，不得在线双读或由 Gateway 读取工作区文件。

迁移 MUST 覆盖显式选定的文件、SQLite 来源层及 active snapshot，Gateway 使用 v3、Workspace 使用 v2。预览 MUST 只读，不创建缺失数据库；来源与 active 的差异和未结算的候选／claim／恢复状态 MUST 显式报告。迁移 MUST 保持既有非日志业务配置的 active／pending 边界，历史事件与已完成 journal 不得改写。

#### Scenario: 旧配置值一致
- **WHEN** 显式迁移输入中的旧 logger 值一致且目标 Gateway 日志配置缺失
- **THEN** 迁移保留该值到 Gateway，移除旧位置并校验两域配置，重复迁移不改变结果

#### Scenario: 新旧值或不同工作区值冲突
- **WHEN** 显式迁移输入的日志设置不一致
- **THEN** 迁移列出来源和差异，要求明确统一值，不静默挑选工作区或覆盖现有目标

#### Scenario: 未迁移的旧工作区后来挂载
- **WHEN** 工作区仍包含旧 logger
- **THEN** 系统明确报告需执行迁移，不忽略该字段，也不把它用于覆盖进程日志

#### Scenario: 已有 active 快照仍包含旧日志配置
- **WHEN** 用户对已有 SQLite 配置状态执行显式日志迁移
- **THEN** 迁移同步处理来源与 active 快照及其校验基线，重启能恢复迁移后的原业务配置，不能仅改文件或顺便激活其他待生效配置

#### Scenario: 迁移预览遇到未结算应用
- **WHEN** 选定来源存在 pending_restart、应用 claim 或 recovery_required
- **THEN** 预览列出实际状态且不修改它们；迁移写入必须等原 owner 结算，不能以迁移为由删除或覆盖
