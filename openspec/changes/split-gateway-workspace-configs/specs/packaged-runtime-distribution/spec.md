## MODIFIED Requirements

### Requirement: 自包含 Linux Python runtime
`@boxteam/runtime-linux-x64` SHALL 包含可重定位的 Python 3.12 runtime、锁定的 Python 依赖、BoxTeam 应用代码、Gateway/Workspace 普通 JSONC 模板和对应 schema；安装后启动 MUST NOT 调用系统 Python、uv 或在线下载 Python。

#### Scenario: 隐藏系统 Python
- **WHEN** 把打包后的 npm 产物安装到 PATH 中没有 Python 或 uv 的隔离环境
- **THEN** `boxteam` 使用 manifest 声明的 Python 和静态配置资源初始化双配置并启动 Gateway

### Requirement: 可重定位且经过验证的产物
构建流程 SHALL 在不复制仓库 `.venv` 的情况下创建 runtime staging，并 MUST 在把打包产物移动到不同绝对路径后验证双配置初始化、加载和迁移。

#### Scenario: 重定位 smoke test
- **WHEN** 把构建后的 runtime 解压到不同于构建路径的位置
- **THEN** Gateway/Workspace 配置初始化、Gateway 健康检查、工作区路由、安全重启后端和关闭全部成功，且运行时不访问源码配置
