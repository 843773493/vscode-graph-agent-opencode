## ADDED Requirements

### Requirement: 自包含 Linux Python runtime
`@boxteam/runtime-linux-x64` SHALL（必须）包含可重定位的 Python 3.12 runtime、锁定的 Python 依赖和 BoxTeam 应用代码；安装后启动 MUST NOT（不得）调用系统 Python、uv 或在线下载 Python。

#### Scenario: 隐藏系统 Python
- **WHEN** 把打包后的 npm 产物安装到 PATH 中没有 Python 或 uv 的隔离环境
- **THEN** `boxteam` 使用 manifest 声明的 Python 初始化配置并启动 Gateway

### Requirement: 打包 Chromium
Linux 平台 runtime SHALL（必须）包含 Browser Manager 所需且与 Playwright 兼容的 Chromium 可执行文件，并 MUST NOT（不得）在应用启动时下载浏览器。

#### Scenario: 浏览器 smoke test
- **WHEN** Browser Manager 在安装后的 runtime 中创建浏览器
- **THEN** Browser Manager 启动 manifest 声明的已打包 Chromium，并完成基本页面操作

### Requirement: 平台包选择
公开的 `boxteam` 包 SHALL（必须）通过 npm 平台元数据选择 `@boxteam/runtime-linux-x64`，并 SHALL（必须）报告不支持的平台，不得使用不兼容 runtime。

#### Scenario: 安装到 Linux x64
- **WHEN** npm 在 Linux x64 上安装 `boxteam`
- **THEN** Launcher 解析匹配的公开平台包

#### Scenario: 不支持的平台
- **WHEN** 没有安装匹配的平台 runtime
- **THEN** `boxteam` 报告检测到的 OS 和 CPU 后退出，并且不搜索系统 Python

### Requirement: 生产静态 Web UI
安装后的 Gateway SHALL（必须）从自身 HTTP origin 提供已打包 Web UI，同时保持 API、SSE、WebSocket 和辅助代理路由。

#### Scenario: 请求安装版根路径
- **WHEN** 用户打开安装版 Gateway 根 URL
- **THEN** Gateway 返回已打包 Web UI

#### Scenario: 未知 API 路由
- **WHEN** 请求指向未知 `/api/` 路由
- **THEN** Gateway 返回 API 错误，而不是 SPA index

### Requirement: 可重定位且经过验证的产物
构建流程 SHALL（必须）在不复制仓库 `.venv` 的情况下创建 runtime staging，并 MUST（必须）在把打包产物移动到不同绝对路径后进行验证。

#### Scenario: 重定位 smoke test
- **WHEN** 把构建后的 runtime 解压到不同于构建路径的位置
- **THEN** 配置初始化、Gateway 健康检查、工作区路由、安全重启后端和关闭全部成功

### Requirement: 可复现的本地与 CI 打包
本地打包和 CI 打包 SHALL（必须）调用同一构建入口、以 Linux x64 GNU 为目标，并生成版本匹配的主包和平台包 tarball，默认不发布。

#### Scenario: 本地构建包
- **WHEN** 开发者运行文档指定的打包命令
- **THEN** 构建流程生成可安装的 `boxteam` 和 `@boxteam/runtime-linux-x64` tarball，并在不发布 npm 的情况下完成验证

### Requirement: 未来独立 Node 扩展点
发行 manifest 和文档 SHALL（必须）为未来 Windows `.exe` 与 Linux 安装程序预留 bundled Node 可执行文件位置；当前 npm runtime 继续使用 Launcher Node。

#### Scenario: 当前 npm 包
- **WHEN** npm 发行版启动 Node 服务
- **THEN** 发行版使用 Launcher Node，并在诊断信息中报告其版本

#### Scenario: 请求独立安装程序打包
- **WHEN** 未来的 standalone builder 使用 manifest schema
- **THEN** schema 可以声明 bundled Node 路径，而无需改变 Gateway 或工作区服务所有权
