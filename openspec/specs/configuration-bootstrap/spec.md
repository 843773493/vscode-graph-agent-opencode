## Purpose

定义 BoxTeam 用户配置从首次安装到日常启动的完整生命周期，包括缺失初始化、显式强制重建、版本化 schema 安装、源码开发隔离，以及多来源配置合并后的有效优先级与热重载判定。

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
仅开发使用的工具和测试工作区 MUST（必须）由显式 development overlay 提供，并 MUST NOT（不得）写入正常安装的用户配置。

#### Scenario: 源码开发启动
- **WHEN** development profile 启动
- **THEN** development overlay 参与有效配置合并，同时保持安装配置不变

### Requirement: 有效配置优先级
runtime SHALL（必须）保持用户配置与工作区配置的优先级，并且仅在最终有效配置发生变化时触发重载操作。

#### Scenario: 被遮蔽的低优先级修改
- **WHEN** 低优先级来源修改了仍被工作区配置覆盖的值
- **THEN** 有效 revision 和重载状态保持不变

#### Scenario: 必须重启的配置段变化
- **WHEN** 有效 MCP 或 logger 配置发生变化
- **THEN** 后端报告需要重启，而不是部分应用候选配置
