## Purpose

定义 BoxTeam 用户配置从首次安装到日常启动的完整生命周期，包括缺失初始化、显式强制重建、版本化 schema 安装、源码开发配置安装，以及统一配置优先级与热重载判定。

## Requirements

### Requirement: 仅在缺失时初始化配置
配置初始化 SHALL（必须）仅在用户配置不存在时创建配置；普通启动 MUST NOT（不得）重新生成或覆盖现有用户配置。

#### Scenario: 安装后首次启动
- **WHEN** `boxteam` 启动且用户配置不存在
- **THEN** 内置生成器在 Gateway 启动前以原子方式创建配置及同目录 schema

#### Scenario: 配置已经存在
- **WHEN** `boxteam` 启动且用户配置已经存在
- **THEN** 启动流程验证并加载配置，不重写其内容

### Requirement: 显式执行破坏性重建
完整配置重建 MUST（必须）要求显式 force 命令，并 MUST（必须）在替换前明确目标位置。

#### Scenario: 强制重建
- **WHEN** 用户运行配置初始化的 force 形式
- **THEN** 完整生成的配置以原子方式替换目标文件，命令报告解析后的路径

### Requirement: 安装打包的 schema
源码发行和安装发行 SHALL（必须）在用户配置旁安装相同版本的 JSON schema，且不依赖源码仓库路径。

#### Scenario: npm 配置引导
- **WHEN** 从 npm runtime 初始化配置
- **THEN** schema 内容来自已打包的 runtime 资源，且 `$schema` 解析到配置同目录

### Requirement: 开发配置隔离
源码开发启动 MUST（必须）在进程启动前将源码 `.env`、静态 `gateway_dev.jsonc` 与静态 `workspace_dev.jsonc` 安装到 development `BOXTEAM_HOME`。运行期 MUST NOT（不得）直接读取源码配置，且不得修改正常安装使用的 `BOXTEAM_HOME`。

#### Scenario: 源码开发启动
- **WHEN** development profile 启动
- **THEN** 启动器原子替换 development home 中的 `.env`、`gateway.jsonc`、`workspace.jsonc` 及各自 schema，再通过与安装发行相同的配置加载路径启动 Launcher

#### Scenario: 源码开发配置无效
- **WHEN** 源码 `.env`、任一开发模板或对应 schema 缺失或校验失败
- **THEN** development profile 在启动任何服务前失败，并报告源配置路径

### Requirement: 统一运行时配置来源
development、源码安装和 npm 安装的 Gateway runtime MUST（必须）只从 `${BOXTEAM_HOME}/config/.env` 与 `${BOXTEAM_HOME}/config/gateway.jsonc` 加载控制面配置；Workspace runtime MUST（必须）只从 `${BOXTEAM_HOME}/config/workspace.jsonc` 以及当前显式工作区 `.boxteam/workspace.jsonc` 加载业务配置。

#### Scenario: 运行源码开发版本
- **WHEN** Gateway 或工作区后端在 development profile 中启动
- **THEN** runtime 不访问项目根 `.env`、`configs/gateway_dev.jsonc` 或 `configs/workspace_dev.jsonc`

### Requirement: 有效配置优先级
runtime SHALL（必须）保持用户配置与工作区配置的优先级，并且仅在最终有效配置发生变化时触发重载操作。

#### Scenario: 迁移旧 SSH 直连 Gateway 配置
- **WHEN** v3 配置包含 `kind: ssh` 或旧的远程后端直连字段
- **THEN** runtime 将其迁移为 `kind: remote_gateway`、补充 `remote_gateway_port` 并删除新 schema 不再支持的直连字段

#### Scenario: 被遮蔽的低优先级修改
- **WHEN** 低优先级来源修改了仍被工作区配置覆盖的值
- **THEN** 有效 revision 和重载状态保持不变

#### Scenario: 必须重启的配置段变化
- **WHEN** 有效 MCP 或 logger 配置发生变化
- **THEN** 后端报告需要重启，而不是部分应用候选配置
