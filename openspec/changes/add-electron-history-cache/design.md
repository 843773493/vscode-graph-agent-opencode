## Context

Electron 和 electron-web 当前只有边界说明；完整 Workbench 已复用 web 的聊天与 IndexedDB 镜像。参见 proposal.md。

## Goals / Non-Goals

**Goals:** 完整 Electron 构建与便携打包、产物迁移启动、全部 Web 功能复用、稳定前端存储、正常关闭提交、真实产物验收。

**Non-Goals:** 自动更新、所有操作系统验收、后端自启动、新聊天 UI、原生文件系统 IPC、原生扩展体系、其他系统能力、把缓存变为权威业务数据。

## Decisions

- electron-web 入口只导入 Web 的相同主入口；同一构建器输出完整静态资源。共享入口提供关闭提交函数，通过原版存储服务保存视图，不另建一套窗口/Tab 存储。
- `packaging/electron` 构建 main ESM bundle 与 renderer，使用 Electron Packager 和固定 Electron 版本生成桌面目录，再产生便携压缩包与 SHA-256。构建产物统一写 `out/packaging/electron`。
- 应用资源一律从 `app.getAppPath()` 定位，自带静态资源并通过与 Web 相同的回环服务器连接已有 Gateway。主进程不依赖源码路径和开发服务器，不引入文件系统 IPC 或另一套网络/业务实现。
- 从现有静态服务器提取唯一 server factory，由 Web CLI 与原生宿主共用，完整保留 HTTP/SSE/WebSocket 代理。
- Electron userData 固定在 `${BOXTEAM_HOME:-~/.boxteams}/clients/electron`，partition 固定为 `persist:boxteam`。首次绑定回环随机空闲端口后记录 `renderer-origin.json`（内部运行态，不是用户配置），以后必须复用；冲突报错。Gateway 使用显式启动参数，不写进镜像。
- 采用普通回环 HTTP，使现有 WebSocket 和原版扩展 worker 保持工作。自定义 scheme 需要另改 WS，当前不引入。服务与应用同生命周期，不暴露外网。
- 无文件系统桥接需求，不新增空 preload 或假 IPC。main 只在关闭时调用 renderer 暴露的固定异步提交函数；renderer 无调用 Node 的能力。关闭前等待全部 pending/in-flight 历史写入，再刷新 Chromium 存储；失败显示错误并保留窗口供重试。
- contextIsolation、sandbox 开启，Node 关闭；每个窗口只允许其初始来源内导航，HTTP(S) 链接沿用 Web 的新窗口行为并保持隔离，拒绝网页创建 webview；不添加系统浏览器等额外原生能力。

## Risks / Trade-offs

- 固定回环端口被占用 → 明确错误，保留原存储，不静默切换。
- 强杀或系统断电可能丢失尚在内存中的短时更新 → 保留现有 500ms 合并写，正常退出明确等待。
- 独立浏览器与 Electron 不共享 profile → 共用实现和身份规则，各自保存镜像，不复制浏览器私有数据。
- 本轮只在 Linux 真正运行 Electron → Windows/macOS 安装与系统能力保留 TODO，不宣称跨平台验收。

## Migration Plan

新增桌面 build/package/start 命令；旧 Web 入口沿用同一启动函数与服务器，无业务协议迁移。移除入口即可回退，持久化镜像不影响后端权威。
