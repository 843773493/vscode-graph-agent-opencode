## ADDED Requirements

### Requirement: 声明式开发目标配置
系统 SHALL（必须）从未提交 Git 的本地配置读取目标标识、provisioner、操作系统、SSH 主机、端口、用户、身份文件、known-hosts 文件、目标 Home 和完整仓库路径，并 SHALL 使用已提交的 JSON schema 和示例配置验证这些字段。系统 MUST NOT（不得）把密钥、密码或真实目标地址硬编码到脚本、Compose 文件或测试中。

#### Scenario: 读取有效目标
- **WHEN** 用户对一个通过 schema 校验的目标标识执行管理命令
- **THEN** 系统使用该目标声明的 SSH 与平台适配配置执行操作

#### Scenario: 目标配置不完整
- **WHEN** 目标缺少平台、SSH host key 校验配置或绝对目标路径
- **THEN** 系统在连接或修改目标之前失败并报告缺失字段

### Requirement: 完整仓库快照同步
系统 SHALL 在不修改宿主机当前分支、工作区和真实 Git index 的前提下，把当前 `HEAD`、已跟踪修改、删除以及未忽略的未跟踪文件组成临时提交，并 SHALL 经 SSH 把新增 Git 对象推送到目标端完整非 bare 仓库的专用快照引用。系统 MUST NOT 自动挂载或复制宿主机仓库作为目标仓库。

#### Scenario: 同步脏宿主机工作区
- **WHEN** 宿主机包含已跟踪修改和未忽略的未跟踪文件
- **THEN** 目标快照包含这些内容，宿主机当前分支、index 和工作区保持不变

#### Scenario: 重复同步
- **WHEN** 前一次快照的大部分 Git 对象已经存在于目标仓库
- **THEN** Git 只传输目标端缺少的对象而不重新复制完整项目

#### Scenario: 子模块存在本地修改
- **WHEN** 已初始化子模块包含未提交修改
- **THEN** 同步在创建主仓库快照前失败并明确指出未被快照包含的子模块

### Requirement: 保护目标端本地开发
系统 SHALL 只把快照推送到非检出引用，并 SHALL 仅在目标工作区没有已跟踪修改、删除和未跟踪文件时激活快照。发现目标工作区脏状态时，系统 MUST 失败并列出冲突路径，MUST NOT 自动 stash、清理、reset 或覆盖目标修改。

#### Scenario: 激活到干净目标仓库
- **WHEN** 快照已推送且目标完整仓库工作区干净
- **THEN** 目标端管理脚本把专用托管分支切换到该快照并报告激活的提交 ID

#### Scenario: 目标端正在开发
- **WHEN** 目标工作区包含本地修改或未跟踪文件
- **THEN** 快照引用可以更新但激活失败，目标文件和当前分支保持不变

### Requirement: 安全同步开发环境文件
系统 SHALL 使用目标声明的同一套 SSH 身份与 host key 校验配置直接复制宿主机最新版 `.env` 到目标仓库，先写临时文件、校验 SHA-256，再按目标平台原子替换。系统 MUST NOT 把 `.env` 加入 Git 快照、输出其内容或在目标缺少有效传输结果时继续启动。

#### Scenario: 更新目标环境文件
- **WHEN** 宿主机 `.env` 存在且通过已验证 SSH 连接完成传输
- **THEN** 目标仓库 `.env` 与宿主机文件哈希一致，临时上传文件被清理

#### Scenario: 环境文件校验失败
- **WHEN** 目标临时文件的 SHA-256 与宿主机不一致
- **THEN** 系统保留原目标 `.env`、删除或报告临时文件并以错误结束

### Requirement: 目标拥有独立开发运行时
每个源码开发目标 SHALL 在自己的完整仓库根目录使用标准 `.venv`，通过目标平台上的 `uv sync --frozen` 构建 Python 环境，并 SHALL 使用 Bun 安装锁定的 JavaScript 依赖。系统 MUST NOT 创建、查找或启动项目根目录下的 `.venv_docker_*`。

#### Scenario: 首次初始化 Linux 目标
- **WHEN** Linux 目标完整仓库尚无 `.venv`
- **THEN** 初始化在该仓库创建 `.venv/bin/python` 并用它运行源码服务

#### Scenario: 首次初始化 Windows 目标
- **WHEN** Windows 目标完整仓库尚无 `.venv`
- **THEN** 初始化在该仓库创建 `.venv\Scripts\python.exe` 并用它运行源码服务

#### Scenario: 保留环境与镜像 Python 不兼容
- **WHEN** 持久化 `.venv` 的平台或 Python 主次版本不再匹配目标运行时
- **THEN** 初始化明确报告失效原因并重建该目标仓库自己的 `.venv`

### Requirement: 持久 Docker 开发目标
Docker provisioner SHALL 在 `out/cross-platform-dev-targets/<target-id>/` 下为目标 Home、完整仓库、缓存、SSH 状态和产物使用明确命名的持久目录，并 SHALL 以不依赖宿主机当前仓库挂载的方式启动容器。容器重建后 SHALL 保留 Git 对象、源码、`.venv`、`.boxteams-dev`、`.boxteams`、默认工作区和 SSH 身份。

#### Scenario: 重建 Docker 容器
- **WHEN** 用户停止、删除并重新创建同一目标 ID 的容器
- **THEN** 目标仓库提交、开发依赖、两套 BoxTeam Home 和默认工作区仍然存在

#### Scenario: 宿主机与容器共同访问持久目录
- **WHEN** 容器在 bind mount 中创建开发文件或测试产物
- **THEN** provisioner 使用确定的 UID/GID 策略保证目标 SSH 用户和宿主机均可管理这些文件

### Requirement: 隔离开发与安装运行配置
源码开发配置 SHALL 默认使用 `~/.boxteams-dev/` 及 `~/.boxteams-dev/boxteam_workspace`，纯净安装配置 SHALL 默认使用 `~/.boxteams/` 及 `~/.boxteams/boxteam_workspace`。两套配置的 Gateway 状态、工作区数据、日志和实例锁 MUST 保持隔离；目标管理工具 MUST 明确选择运行配置，并 MUST NOT 依赖 `.env` 中隐式选择 `BOXTEAM_HOME`。

#### Scenario: 启动源码开发配置
- **WHEN** 用户以 development 配置启动目标源码服务且未提供显式覆盖
- **THEN** 服务使用目标用户的 `.boxteams-dev` 和其中的默认工作区

#### Scenario: 验证纯净安装
- **WHEN** 用户以 installed 配置启动已安装发行版且未提供显式覆盖
- **THEN** 服务使用目标用户的 `.boxteams`，且不读取 `.boxteams-dev` 的 Gateway 状态或工作区业务数据

#### Scenario: 正式 E2E 需要隔离目录
- **WHEN** 仓库正式测试 fixture 显式提供测试专用 `BOXTEAM_HOME` 和工作区
- **THEN** 目标启动服从该显式隔离目录，同时不写入 installed 配置的 `.boxteams`

### Requirement: 平台原生管理与可观察失败
系统 SHALL 提供目标无关的 sync、bootstrap、start、stop、restart、status、shell、test 和 collect 操作，并 SHALL 分别通过 POSIX shell 与 PowerShell 适配 Linux 和 Windows 的路径、进程及文件操作。每个失败 MUST 返回非零状态并包含目标 ID、平台、失败阶段和底层命令错误，不得静默降级。

#### Scenario: 查询 Windows 目标状态
- **WHEN** 用户对 Windows 目标执行 status
- **THEN** 系统通过 PowerShell 获取实际进程和健康状态而不发送 `nohup`、`kill` 等 POSIX 命令

#### Scenario: 目标服务启动失败
- **WHEN** 目标 Python、uv、Bun、端口或运行时资源不满足启动条件
- **THEN** start 返回非零状态并报告具体检查失败项，不显示虚假的运行中状态

### Requirement: 支持真实远程 Gateway 测试
开发目标 SHALL 提供稳定的已认证 SSH 入口，并 SHALL 能在目标本地运行完整 Gateway、Web、Workspace API、Terminal 和 Browser 开发服务，使其他 Gateway 可以通过既有远程 Gateway 联邦流程连接该目标。目标工具 MUST NOT 为测试绕过 Gateway 联邦认证或协议检查。

#### Scenario: 其他 Gateway 导入开发目标
- **WHEN** 另一 Gateway 使用目标 SSH 配置与正在运行的目标 Gateway 配对
- **THEN** 连接经过现有联邦认证、协议协商和远程工作区投影流程

#### Scenario: Docker 目标重建后重新连接
- **WHEN** Docker 容器重建但持久 SSH 状态和 BoxTeam Home 保留
- **THEN** 目标仍可使用已声明 SSH 身份启动，连接错误和需要重新配对的情况被明确报告

### Requirement: 目标无关的 E2E 接入
Gateway 跨端 E2E 辅助层 SHALL 通过统一目标接口执行远程命令、同步文件和查询服务，而 MUST NOT 硬编码 Docker 容器名、`/workspace` 仓库路径或 POSIX 命令。仓库 SHALL 包含带目录说明的 `tests/e2e/windows/` 骨架，但本变更 MUST NOT 要求新增 Windows 测试用例。

#### Scenario: 运行现有 Docker Gateway E2E
- **WHEN** 测试选择一个 platform 为 Linux、provisioner 为 Docker 的目标
- **THEN** 测试通过 SSH 目标接口使用该目标完整仓库和测试隔离目录完成现有路由验证

#### Scenario: 后续增加 Windows 用例
- **WHEN** 后续测试选择 platform 为 Windows 的 VMware 目标
- **THEN** 同一辅助层选择 PowerShell 平台适配器，而测试用例无需引用 Docker Compose 或 POSIX 固定路径
