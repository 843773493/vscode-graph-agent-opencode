## 背景

当前 `scripts/dev.mjs` 同时承担配置安装、端口清理、七个进程的启动和监督，并把默认 Workspace API 作为 Gateway 外部进程注册；Gateway 后续创建的本地工作区却由 Gateway 自己托管。运行时还通过源码根目录定位 Python、Node 服务和配置 schema，无法直接搬入 npm 安装目录。远程工作区经 SSH 分别转发 Workspace API、Terminal 和 Browser，因而本地 Gateway 无法委托远程后端生命周期操作。

首发目标只有 Linux x86_64 GNU：宿主机是 Ubuntu glibc 2.39，测试容器基于 `python:3.12-slim`。npm 用户 `lain14138` 拥有 `boxteam` 组织；主包使用 `boxteam`，平台包使用 `@boxteam/runtime-linux-x64`。源码开发与正式安装分别使用 `~/.boxteams-dev/` 和 `~/.boxteams/`。

## 目标与非目标

**目标：**

- 用同一 Launcher 和 runtime manifest 支撑源码开发、源码安装及 npm 安装。
- 让 npm 安装产物携带可重定位 Python、应用依赖和 Chromium，不依赖系统 Python 或首次运行下载。
- 统一 Gateway 对默认及新增本地工作区的进程所有权，提供可观测、安全、可强制的后端重启。
- 让本地 Gateway 只连接远程 Gateway，由远程 Gateway 管理远程工作区后端。
- 保留开发 HMR，同时让正式 Gateway 直接提供静态 Web UI。
- 为未来自带 Node.js 的 Windows `.exe` 和 Linux 安装程序保留显式、非推测式扩展点。

**非目标：**

- 首发不支持 Windows、macOS、ARM、Alpine/musl、后台 OS service、自更新和产物签名。
- 首发 npm 包不携带 Node.js，运行时复用执行 Launcher 的 Node。
- 不在本变更中支持无限递归的 Gateway 联邦或跨 Gateway 高可用切换。
- 不默认执行 `npm publish`，只生成可本地安装和验证的 tarball。

## 设计决策

### 1. 使用薄 Launcher 和显式 runtime manifest

发布包中的 `boxteam` 只负责命令解析、实例锁、配置 bootstrap、Gateway 子进程监督、信号转发和浏览器打开。所有资源位置由版本化 manifest 给出，包括 Python、应用根目录、Web 资产、Chromium和 Node 来源。

不从当前目录、`.git`、`pyproject.toml` 或脚本文件父目录推测发行形态。development manifest 显式引用仓库 `.venv` 和源码资源；installed manifest 只引用包内相对路径。

Node provider 使用 `launcher` 或 `bundled` 两种枚举。npm 首发选择 `launcher`；未来独立安装程序选择 `bundled` 并提供相对可执行路径。代码和架构说明保留未来实现 TODO，但不添加无效回退逻辑。

### 2. Launcher 只拥有 Gateway，Gateway 拥有工作区服务

Launcher 是 Gateway 的父监督进程；Gateway 统一启动默认工作区及所有本地托管工作区的 Workspace API、Terminal Manager 和 Browser Manager。`dev.mjs` 缩减为生成 development manifest、启动 Vite 并调用 Launcher，不再单独启动默认 8010 后端。

`WorkspaceRuntime` 从无类型的进程数组改为按服务命名的 handle。重启 Workspace API 时保留 Terminal 和 Browser；删除工作区或关闭 Gateway 时才关闭整套运行时。进程 handle 使用进程组，先发优雅终止信号，超时后再终止整个进程组。

### 3. 后端重启采用 drain 状态机

Workspace API 增加运行时生命周期协调服务，状态依次为 `ready`、`draining`、`stopping`。进入 `draining` 后拒绝新 Job，并返回真实的活动 Job、工具执行和待写入状态。Gateway 默认等待最多 30 秒；超时后保持旧后端运行并向 UI 返回 blockers。

只有显式 force 请求才能中断活动任务。强制前先持久化 `interrupted` 状态和重启原因。启动时执行状态对账，任何没有实际执行载体却仍为 `running` 的记录都必须变成可观察的中断状态。

重启过程先排空并停止旧 Workspace API，再启动新 Workspace API，避免两个完整后端同时读写同一个 `.boxteam/`。若将来需要低停机切换，新进程必须先支持不启动调度器、不接受任务的 standby 模式，本变更不实现该模式。

### 4. 远程连接升级为 Gateway 联邦

SSH 只转发远程 Gateway 的 loopback 端口。连接建立后，本地 Gateway 调用带协议版本的远程 manifest/workspaces API，并把每个远程工作区表示为引用 `remote_gateway_connection_id` 和 `remote_workspace_id` 的投影视图。

普通业务请求发送到远程 Gateway，并携带远程 workspace ID；本地不直接保存或代理远程 Workspace API 临时端口。重启请求同样委托给远程 Gateway，远程 Gateway 根据目标是否由其托管决定安全重启或拒绝。

Gateway-to-Gateway 使用独立随机凭据。首次连接利用已认证 SSH 会话执行远程配对命令，获取短期、受作用域限制的连接凭据；凭据只存放在 Gateway 控制面目录且限制文件权限。浏览器本地 token 与联邦 token 分离，日志和 DTO 不返回密钥。首发只导出远程 Gateway 直接管理的工作区，阻止联邦递归和循环。

### 5. 配置 bootstrap 与启动解耦

默认配置的唯一源仍由 Python 构造。构建阶段生成默认配置和 schema 资产；Launcher 首次运行时用内置 Python 执行 `config init`。`init` 只在用户配置缺失时创建，`--force` 才允许整文件重建；普通 `start` 仅验证并加载。

源码开发默认设置 `BOXTEAM_HOME=~/.boxteams-dev`，开发专属设置通过显式 overlay 加载，不写入正式用户配置。正式安装始终使用 `~/.boxteams`。工作区配置继续覆盖用户配置，被高优先级遮蔽的低优先级变化不触发有效 revision。

### 6. 正式 Gateway 提供静态 UI

installed manifest 指向构建后的 `src/web/dist`。Gateway 在 API、SSE、WebSocket 和辅助代理路由之后挂载静态资源，并对前端路由执行 index fallback，但绝不把未知 `/api/*` 返回为 HTML。开发模式继续由 Vite 8011 代理 Gateway 8014。

### 7. npm 主包与平台运行时分离

公开主包 `boxteam` 声明 `@boxteam/runtime-linux-x64` 为按 `os`/`cpu` 过滤的 optional dependency。平台包包含固定 Python 3.12 patch 版本、预装 Python packages、应用源码、生产 Node dependencies、Playwright Chromium、Web dist 和 manifest。

首发运行时在 Debian slim 基线上构建，以兼容测试容器和更新的 Ubuntu 宿主机。构建不复制仓库 `.venv`，而是在 staging 中从锁文件安装依赖。验证必须在移动后的临时路径中、从 PATH 隐藏系统 Python/uv 后完成配置初始化、Gateway 启动、静态 UI、后端重启和无残留退出测试。

## 风险与权衡

- [Python 与 Chromium 使平台包较大] → 第一版不设硬性体积上限，先保证完整性；记录各目录体积并在后续按真实数据裁剪。
- [动态 Python tool factory 不适合完全冻结] → 使用可重定位 CPython 加应用源码和 site-packages，不采用单文件冻结。
- [旧 SSH 配置无法无损推导远程 Gateway 地址] → 作为显式 breaking migration，开发配置重新生成，其他旧配置启动时给出包含字段迁移建议的错误。
- [Gateway 联邦扩大认证攻击面] → 只经 SSH loopback tunnel 建连，使用独立短期凭据、协议版本和最小作用域。
- [强制重启时外部工具子进程可能残留] → 所有托管进程使用进程组，并在对账中记录中断；测试验证退出后无监听和无子进程。
- [大量现有未提交修改与本变更重叠] → 把当前工作树视为基线，逐文件读取并做局部补丁，不 reset、不覆盖无关修改。

## 迁移计划

1. 先建立配置 bootstrap、manifest 数据模型、实例锁和 Launcher 骨架，不改变现有开发启动结果。
2. 将 Gateway 运行时拆为命名 service handles，并接管默认工作区；完成后移除 `dev.mjs` 对默认后端和辅助后端的直接所有权。
3. 实现 Workspace API drain/reconciliation 和 Gateway 安全重启 API，再更新控制台交互。
4. 增加生产静态 UI，建立 source-installed staging 并用 development/installed manifests 验证一致性。
5. 将 SSH 直连后端替换为远程 Gateway 协议，同时改造 Docker E2E 为容器内完整远程 Gateway。
6. 构建可重定位 Python、Chromium和 npm 两类 tarball，执行隔离安装测试。
7. 保留旧启动脚本一个开发迁移阶段；新链路验证通过后删除重复进程编排，不保留运行时兼容分支。

回滚以阶段为单位：每阶段都保持前一入口可执行；注册表 schema 写入前保留原文件备份，远程配置 breaking migration 不自动覆写用户文件。npm 发布不在本变更自动执行，因此打包阶段失败不会影响已安装用户。

## 待确认问题

- 可重定位 CPython 的具体上游发行源、固定 patch 版本及许可清单在 packaging 原型阶段通过 relocation smoke test 后锁定。
- Chromium 精简范围以真实打包体积和 Playwright smoke test 为准，不预先删除运行所需资源。
