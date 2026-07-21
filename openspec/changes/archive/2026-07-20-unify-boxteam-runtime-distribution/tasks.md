## 1. Runtime 与配置基础

- [x] 1.1 补充发行方式、进程所有权、manifest 字段、数据根目录及未来 bundled Node 可执行文件的架构说明
- [x] 1.2 拆分配置默认值、安装逻辑和开发资源，保留精简的 `configs.boxteam` 入口
- [x] 1.3 实现仅在缺失时初始化配置、显式强制重建和打包 schema 查找，并添加单元测试
- [x] 1.4 添加开发配置 overlay，并让源码开发默认使用 `~/.boxteams-dev/`
- [x] 1.5 定义并验证适用于开发和安装发行版的版本化 runtime manifest

## 2. Launcher 与开发入口

- [x] 2.1 创建 `boxteam` Launcher 包、精简 bin 入口及目录所需的 AGENTS.md
- [x] 2.2 实现 runtime 包发现、manifest 资源解析及 launcher/bundled Node provider 处理
- [x] 2.3 实现前台 Gateway 监督、信号转发、浏览器打开和明确的启动诊断
- [x] 2.4 实现基于 `BOXTEAM_HOME` 的实例锁和 `boxteam doctor` 诊断
- [x] 2.5 重构 `scripts/dev.mjs`，在保留 Vite/HMR 和调试支持的同时调用 development Launcher
- [x] 2.6 添加 Launcher 单元测试和进程清理测试

## 3. Gateway 本地 runtime 所有权

- [x] 3.1 创建包含专用 AGENTS.md 的 `app/gateway/runtime/`，并将进程与服务生命周期职责迁入聚焦模块
- [x] 3.2 使用具名 Workspace API、Terminal 和 Browser service handle 替换无类型 runtime 进程列表
- [x] 3.3 实现感知进程组的优雅终止和强制清理，并添加单元测试
- [x] 3.4 让 Gateway 启动并拥有默认本地工作区 runtime，不再注册外部 8010 后端
- [x] 3.5 后端单独重启时保留辅助服务，移除工作区或关闭 Gateway 时关闭所有服务
- [x] 3.6 迁移 registry/runtime 状态，并更新本地托管和外部 runtime action DTO 与测试

## 4. 安全重启后端

- [x] 4.1 实现 Workspace API 生命周期状态、关闭新请求入口和真实活动 blocker 报告
- [x] 4.2 添加 Job、工具和后台任务排空 hook，并在显式强制重启时持久化 interrupted 状态
- [x] 4.3 在 Workspace API 启动时对账过期的 running 执行状态
- [x] 4.4 实现 Gateway 安全重启编排，包括 30 秒超时、每工作区锁和健康验证
- [x] 4.5 添加相互独立的安全重启和强制重启 Gateway API，并返回 request ID 和结构化 blocker
- [x] 4.6 更新 Gateway 控制台标签、确认流程、等待状态及 blocker/错误展示
- [x] 4.7 添加后端生命周期单元测试、API 测试和浏览器交互测试

## 5. 安装版静态 Web UI

- [x] 5.1 根据 manifest 在 Gateway 挂载静态 Web UI，并为 `/api` 之外的路径提供 SPA fallback
- [x] 5.2 确保 API、SSE、WebSocket 和辅助代理路由优先于静态资源
- [x] 5.3 添加安装版静态 UI 健康与路由测试，并执行要求的浏览器前端构建

## 6. 远程 Gateway 联邦

- [x] 6.1 定义远程 Gateway 连接、投影工作区、协议 manifest 和持久化 schema
- [x] 6.2 使用生成的本地凭据替换固定 Gateway 凭据，并单独存储联邦凭据
- [x] 6.3 实现经 SSH 认证的远程 Gateway 配对和单一 Gateway 隧道
- [x] 6.4 实现兼容版本的直接托管远程工作区发现，并拒绝循环和嵌套
- [x] 6.5 通过拥有工作区的远程 Gateway 路由远程业务、SSE 和辅助请求，同时保留 request ID
- [x] 6.6 将远程探测、安全重启和强制重启委托给远程 Gateway
- [x] 6.7 替换 SSH 直连后端配置字段，并为旧配置提供明确迁移错误
- [x] 6.8 更新 UI 连接术语、远程层级和 runtime action
- [x] 6.9 重写 Gateway 单元测试和 Docker E2E，使其运行完整远程 Gateway，而不是直接托管远程后端

## 7. Linux x64 打包 runtime

- [x] 7.1 创建 packaging/runtime 构建模块、AGENTS.md 和 staging 布局，不复制仓库 `.venv`
- [x] 7.2 选择并固定可重定位的 Python 3.12 Linux x64 GNU runtime，记录许可和版本元数据
- [x] 7.3 把锁定的 Python 依赖及应用和配置资源安装到可重定位 staging
- [x] 7.4 构建并放置生产 Web UI、Node 服务依赖和 Playwright Chromium
- [x] 7.5 生成使用 Launcher Node 的安装版 runtime manifest，并记录未来 bundled Node 安装程序的 TODO
- [x] 7.6 创建公开的 `boxteam` 和 `@boxteam/runtime-linux-x64` npm 包元数据，保证版本匹配并设置平台过滤
- [x] 7.7 在本地生成 npm tarball 而不发布，并报告 staging 和包内组件体积
- [x] 7.8 在隐藏系统 Python/uv 的搬迁环境中验证安装，包括配置引导、静态 UI、Browser Manager、后端重启和干净关闭
- [x] 7.9 添加 GitHub Actions Linux x64 包构建和产物验证，并复用本地构建入口

## 8. 最终验证与交付

- [x] 8.1 执行 Python 静态分析，以及配置、Gateway、生命周期和联邦的聚焦单元测试
- [x] 8.2 执行所有受影响 Web 构建、TypeScript 检查和聚焦前端测试
- [x] 8.3 通过 8011 执行完整本地 Web 产品初始化检查，并通过 8014 检查安装版 Gateway
- [x] 8.4 执行重写后的 Docker 远程 Gateway E2E 套件和打包 runtime smoke test
- [x] 8.5 对新目录、AGENTS.md 覆盖、顶层符号和文件增长执行架构审查
- [x] 8.6 更新 OpenSpec 任务证据、严格验证变更、同步规格并归档已完成变更
