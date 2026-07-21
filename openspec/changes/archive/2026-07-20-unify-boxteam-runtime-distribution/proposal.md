## 背景与动机

BoxTeam 当前只有依赖源码仓库布局的 `dev.mjs` 启动链路，默认工作区与其他托管工作区的进程所有权不一致，配置会在开发启动时被整文件重建，也没有可直接发布为 npm 包的自包含 Python 运行时。需要统一源码开发、源码安装和 npm 安装的运行语义，使 Gateway 能安全管理本地及远程 Gateway 下的工作区后端，并让 `npm install -g boxteam && boxteam` 在目标 Linux 系统上无需系统 Python 即可运行。

## 变更内容

- 新增统一的 BoxTeam Launcher；`boxteam` 默认以前台方式启动服务并打开 Gateway Web UI，源码开发通过显式 development manifest 复用相同启动边界。
- 源码开发默认使用 `~/.boxteams-dev/`，正式安装使用 `~/.boxteams/`，同一个 `BOXTEAM_HOME` 只允许一个 Gateway 实例。
- 正式模式由 Gateway 直接提供构建后的 Web UI；开发模式继续使用 Vite/HMR。
- Gateway 统一拥有默认工作区和所有本地托管工作区的后端生命周期，并支持带排空、超时和显式强制确认的后端安全重启。
- **BREAKING** 将当前“SSH 直连远程 Workspace API/Terminal/Browser”模型替换为“本地 Gateway 经 SSH 隧道连接远程 Gateway”；远程后端发现、路由和重启均委托给远程 Gateway。
- 将固定本地 token 替换为安装级随机凭据，并为 Gateway-to-Gateway 协议增加独立认证和协议版本检查。
- 配置生成从每次启动动作改为缺失时初始化或显式重建；普通启动只读取、合并和验证配置。
- 新增 `boxteam` npm 主包和 `@boxteam/runtime-linux-x64` 平台运行时包；平台包包含可重定位 CPython、锁定的 Python 依赖、应用代码、Playwright Chromium、Node 服务源码、静态 Web UI 和 runtime manifest。
- npm 发行版暂时复用执行 Launcher 的 Node.js；runtime manifest 为未来自带 Node.js 的 Windows `.exe` 和 Linux 安装程序预留显式运行时位置。
- 本机与 CI 使用同一 staging/build/relocation verification 流程，只发布未签名的 Linux x64 GNU 开发产物；发布动作不属于默认构建流程。

## 能力范围

### 新增能力

- `runtime-launcher`: 统一 Launcher、运行时 manifest、前台监督、实例锁和开发/正式数据隔离。
- `managed-backend-lifecycle`: Gateway 对本地托管后端的所有权、安全排空、重启、状态对账和强制重启规则。
- `remote-gateway-federation`: 通过 SSH 隧道连接远程 Gateway，并委托工作区发现、代理和后端生命周期操作。
- `configuration-bootstrap`: 用户配置的缺失初始化、显式重建、schema 安装、验证及启动时只读规则。
- `packaged-runtime-distribution`: 自带 Python 和 Chromium 的 Linux x64 npm 平台运行时、静态 UI、构建验证及未来 bundled Node 扩展点。

### 修改的能力

<!-- 当前仓库没有既有 OpenSpec capability；本变更全部以新 capability 建立基线。 -->

## 影响范围

- 影响 `scripts/dev.mjs`、根构建脚本、配置生成器、Gateway 启动/认证/注册表/代理/SSH 模块、Workspace API 运行时退出协调以及浏览器前端控制台。
- 新增 Launcher npm package、Linux x64 平台 runtime package、runtime manifest、生产静态资源服务和 packaging/CI 流程。
- 远程工作区配置 schema、Gateway API DTO、持久化注册表 schema 和 E2E Docker 测试需要同步迁移。
- npm 主包为公开的 `boxteam`，平台包为公开的 `@boxteam/runtime-linux-x64`；只有显式发布流程可以访问 npm 发布凭据。
