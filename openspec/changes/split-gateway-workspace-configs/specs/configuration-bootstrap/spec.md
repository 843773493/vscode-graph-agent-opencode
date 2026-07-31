## MODIFIED Requirements

### Requirement: 仅在缺失时初始化配置
配置初始化 SHALL 仅在 Gateway 或 Workspace 用户配置不存在时，从对应普通静态 JSONC 模板创建该配置及同目录 schema；普通启动 MUST NOT 重新生成或覆盖现有文件。

#### Scenario: 安装后首次启动
- **WHEN** `boxteam` 启动且 Gateway 与 Workspace 用户配置都不存在
- **THEN** 资源安装器在 Gateway 启动前原子复制两个普通配置模板及两个 schema

#### Scenario: 部分配置已经存在
- **WHEN** 一个用户配置已经存在而另一个缺失
- **THEN** 启动流程保留已有文件，只初始化缺失配置并验证两个配置域

### Requirement: 显式执行破坏性重建
完整配置重建 MUST 要求显式 force 命令，并 MUST 在替换前明确 Gateway、Workspace 配置及 schema 的全部目标位置。

#### Scenario: 强制重建
- **WHEN** 用户运行配置初始化的 force 形式
- **THEN** 两个普通静态配置和两个 schema 以原子单文件替换方式写入，命令报告解析后的全部路径

### Requirement: 安装打包的 schema
源码发行和安装发行 SHALL 在 Gateway 与 Workspace 用户配置旁安装匹配版本的独立 JSON schema，且不依赖源码仓库路径。

#### Scenario: npm 配置引导
- **WHEN** 从 npm runtime 初始化配置
- **THEN** 两个 schema 和两个普通配置模板来自已打包的 runtime 资源，且每个 `$schema` 解析到配置同目录

### Requirement: 开发配置隔离
源码开发启动 MUST 在进程启动前将源码 `.env`、完整 `gateway_dev.jsonc`、完整 `workspace_dev.jsonc` 和两个 schema 安装到 development `BOXTEAM_HOME`。运行期 MUST NOT 读取源码 `.env` 或源码配置模板，且不得修改正常安装使用的 `BOXTEAM_HOME`。

#### Scenario: 源码开发启动
- **WHEN** development profile 启动
- **THEN** 启动器原子替换 development home 中的 `.env`、两个完整配置和两个 schema，再通过与安装发行相同的加载路径启动 Launcher

#### Scenario: 源码开发配置无效
- **WHEN** 源码 `.env`、任一 development 配置或 schema 缺失或校验失败
- **THEN** development profile 在启动任何服务前失败，并报告具体源路径

### Requirement: 统一运行时配置来源
development、源码安装和 npm 安装的 runtime MUST 只从 `${BOXTEAM_HOME}/config/.env`、`${BOXTEAM_HOME}/config/gateway.jsonc`、`${BOXTEAM_HOME}/config/workspace.jsonc` 以及当前工作区 `.boxteam/workspace.jsonc` 加载配置。

#### Scenario: 运行源码开发版本
- **WHEN** Gateway 或 Workspace Backend 在 development profile 中启动
- **THEN** runtime 不访问项目根 `.env` 或 `configs/*_dev.jsonc`

### Requirement: 有效配置优先级
Workspace runtime SHALL 保持用户级 Workspace 配置与显式工作区配置的优先级，并且仅在最终有效配置发生变化时触发重载操作；Gateway 配置不参与该合并。

#### Scenario: 被遮蔽的低优先级修改
- **WHEN** 用户级 Workspace 配置修改了仍被工作区配置覆盖的值
- **THEN** 有效 revision 和重载状态保持不变

#### Scenario: 必须重启的配置段变化
- **WHEN** 有效 MCP 或 logger 配置发生变化
- **THEN** 后端报告需要重启，而不是部分应用候选配置
