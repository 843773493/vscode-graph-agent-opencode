## MODIFIED Requirements

### Requirement: 统一前台 Launcher
`boxteam` 命令 SHALL 默认在前台启动 BoxTeam、在启动 Gateway 前完成双配置初始化或布局迁移、监督 Gateway 进程、在未显式禁用时打开本地 Web UI，并转发终止信号以执行优雅关闭。

#### Scenario: 不带子命令启动
- **WHEN** 用户使用有效的已安装 runtime 运行 `boxteam`
- **THEN** Launcher 完成 Gateway 与 Workspace 配置准备后，在前台启动 Gateway 并打开 Gateway Web UI

#### Scenario: 配置准备失败
- **WHEN** 双配置初始化、迁移或验证失败
- **THEN** Launcher 在创建任何服务进程前退出并报告具体配置路径和原因

#### Scenario: 用户终止 Launcher
- **WHEN** 前台 Launcher 收到中断或终止信号
- **THEN** Launcher 请求 Gateway 优雅关闭、等待托管服务退出，并且不遗留托管子进程

### Requirement: 显式 runtime manifest
Launcher MUST 从版本化 runtime manifest 解析 Python、应用资源、Web 资源、Chromium、Node provider 和双配置资源行为，并 MUST NOT 根据当前工作目录或源码仓库标记推断已安装 runtime。

#### Scenario: 在仓库外启动已安装 runtime
- **WHEN** 从任意工作目录执行 `boxteam`
- **THEN** 所有 runtime 资源及 Gateway/Workspace 配置模板均从已安装 manifest 解析，启动不要求该目录包含 `pyproject.toml`

#### Scenario: Runtime 配置资源缺失
- **WHEN** manifest 声明的任一配置模板或 schema 不存在
- **THEN** 启动失败，并报告 manifest 路径、缺失资源和发行标识
