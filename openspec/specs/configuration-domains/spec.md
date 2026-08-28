## Purpose

定义 Gateway 与 Workspace 配置的运行时所有权、独立 schema 与版本、旧合并布局迁移以及迁移后的配置读取边界。

## Requirements

### Requirement: 配置按运行时所有权隔离
Gateway SHALL 只读取 `${BOXTEAM_HOME}/config/gateway.jsonc`，Workspace Backend SHALL 读取 `${BOXTEAM_HOME}/config/workspace.jsonc` 并叠加当前显式工作区的 `.boxteam/workspace.jsonc`。Gateway MUST NOT 读取任何工作区级配置。

#### Scenario: 同一 Gateway 管理多个工作区
- **WHEN** Gateway 注册同一台电脑上的多个 Workspace Backend
- **THEN** Gateway 只依据用户级 Gateway 配置和控制面状态路由，不读取任一工作区的 `.boxteam/workspace.jsonc`

#### Scenario: Workspace 应用本地覆盖
- **WHEN** Workspace Backend 启动且显式工作区存在 `.boxteam/workspace.jsonc`
- **THEN** 后端先加载用户级 Workspace 配置，再用该显式工作区配置覆盖同名项

### Requirement: 配置域使用独立 schema 和版本
Gateway 与 Workspace 配置 SHALL 各自使用独立 schema 和 `config_version`，schema MUST 拒绝属于另一配置域的字段。

#### Scenario: Workspace 配置包含 Gateway 字段
- **WHEN** 用户级或工作区级 Workspace 配置包含 `gateway`
- **THEN** Workspace 配置验证失败并报告文件路径与字段位置

#### Scenario: Gateway 配置包含 Agent 字段
- **WHEN** Gateway 配置包含 `agents`、`llm` 或其他 Workspace 业务字段
- **THEN** Gateway 配置验证失败并报告文件路径与字段位置

### Requirement: 旧合并配置执行可恢复布局迁移
Launcher SHALL 在服务启动前把旧用户级 `boxteam.jsonc` 拆分为新 Gateway 与 Workspace 配置，并把旧工作区级 `boxteam.jsonc` 迁移为 `workspace.jsonc`。迁移 MUST 验证全部候选，MUST 可在进程中断后幂等恢复，且 MUST NOT 静默覆盖冲突的新文件。

#### Scenario: 迁移旧用户配置
- **WHEN** 旧用户级 `boxteam.jsonc` 存在且两个新目标不存在
- **THEN** 系统先完成旧版本内容迁移，再生成并验证 `gateway.jsonc` 与 `workspace.jsonc`，确认两个目标后移除旧源

#### Scenario: 提交一个目标后中断
- **WHEN** 布局迁移在一个新目标提交后进程中断
- **THEN** 下一次启动依据迁移状态和内容摘要继续完成迁移，不覆盖不匹配的文件

#### Scenario: 新旧配置发生冲突
- **WHEN** 旧源与用户独立创建的新目标同时存在且内容不能证明一致
- **THEN** 启动失败并报告全部冲突路径，不猜测优先级

#### Scenario: 旧工作区配置越权声明 Gateway
- **WHEN** 工作区级旧 `boxteam.jsonc` 包含 `gateway`
- **THEN** 布局迁移失败并说明 Gateway 配置只能位于用户级 Gateway 文件

### Requirement: 迁移后不读取旧布局
布局迁移完成后，运行时 MUST NOT 把 `boxteam.jsonc`、`boxteam.json` 或源码模板作为配置 fallback。

#### Scenario: 迁移完成后旧文件重新出现
- **WHEN** 新配置已经存在且旧配置文件再次出现
- **THEN** 启动报告布局冲突，不把旧文件合并进有效配置
