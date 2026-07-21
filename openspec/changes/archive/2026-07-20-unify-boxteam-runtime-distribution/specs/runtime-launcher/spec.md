## ADDED Requirements

### Requirement: 统一前台 Launcher
`boxteam` 命令 SHALL（必须）默认在前台启动 BoxTeam、监督 Gateway 进程、在未显式禁用时打开本地 Web UI，并转发终止信号以执行优雅关闭。

#### Scenario: 不带子命令启动
- **WHEN** 用户使用有效的已安装 runtime 运行 `boxteam`
- **THEN** Launcher 在前台启动 Gateway 并打开 Gateway Web UI

#### Scenario: 用户终止 Launcher
- **WHEN** 前台 Launcher 收到中断或终止信号
- **THEN** Launcher 请求 Gateway 优雅关闭、等待托管服务退出，并且不遗留托管子进程

### Requirement: 显式 runtime manifest
Launcher MUST（必须）从版本化 runtime manifest 解析 Python、应用资源、Web 资源、Chromium 和 Node provider 行为，并 MUST NOT（不得）根据当前工作目录或源码仓库标记推断已安装 runtime。

#### Scenario: 在仓库外启动已安装 runtime
- **WHEN** 从任意工作目录执行 `boxteam`
- **THEN** 所有 runtime 资源均从已安装 manifest 解析，启动不要求该目录包含 `pyproject.toml`

#### Scenario: Runtime 资源缺失
- **WHEN** manifest 声明的可执行文件或必需资源不存在
- **THEN** 启动失败，并报告 manifest 路径、缺失资源和发行标识

### Requirement: 按发行方式隔离数据
除非显式提供 `BOXTEAM_HOME`，源码开发 SHALL（必须）默认使用 `~/.boxteams-dev/`，源码安装和 npm 安装发行版 SHALL（必须）默认使用 `~/.boxteams/`。

#### Scenario: 源码开发默认目录
- **WHEN** development Launcher 启动时没有 `BOXTEAM_HOME`
- **THEN** Launcher 使用 `~/.boxteams-dev/` 保存配置和控制面状态

#### Scenario: 安装发行版默认目录
- **WHEN** 已安装 Launcher 启动时没有 `BOXTEAM_HOME`
- **THEN** Launcher 使用 `~/.boxteams/`

### Requirement: 每个 home 只能运行一个 Gateway
Launcher MUST（必须）防止两个受监督的 Gateway 实例同时使用同一 `BOXTEAM_HOME`，并 MUST（必须）在诊断信息中显示所有者进程信息。

#### Scenario: 重复启动
- **WHEN** 第二个 Launcher 尝试使用已锁定的 `BOXTEAM_HOME` 启动
- **THEN** Launcher 报错退出并标识活动实例，而不是终止或替换该实例

### Requirement: 可扩展 Node provider
runtime manifest SHALL（必须）区分 npm Launcher 提供的 Node 可执行文件和未来 bundled Node 可执行文件，并且不回退到任意 PATH 查找。

#### Scenario: npm runtime 使用 Launcher Node
- **WHEN** manifest 声明 Node source 为 `launcher`
- **THEN** Node 服务使用当前 Launcher 的 `process.execPath` 运行

#### Scenario: 未来声明 bundled Node
- **WHEN** 未来 manifest 声明 Node source 为 `bundled`
- **THEN** 解析过程要求使用 manifest 声明的相对可执行文件；在 standalone packaging 实现前，返回明确的尚未支持错误
