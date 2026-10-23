# 目录用途

`Playwright_remote_screen/` 是 Playwright 远程屏幕实验项目，用于验证本地浏览器会话、浏览器控制命令和 Web 端 attach/detach 远程操控能力。

```text
Playwright_remote_screen/
├── package.json            依赖、Node 启动和 Bun 检查命令
├── bun.lock                demo 依赖锁定
├── scripts/check.mjs       浏览器、服务端和测试源码的语法检查
├── src/
│   ├── client/
│   │   ├── main.js                       viewer WebSocket、命令和 viewport 状态
│   │   ├── latestPointerMoveScheduler.js 合并高频 pointer move
│   │   ├── canvasRectCache.js            合并几何读取并刷新离散输入坐标
│   │   └── remoteFrameRenderer.js        解码最新屏幕帧并释放旧图像资源
│   └── server/
│       ├── server.js                     HTTP/WebSocket 入口和关闭顺序
│       ├── browserSession.js             Playwright 页面、CDP、screencast 生命周期
│       ├── browserControlCommands.js     命令解析与 Playwright 动作映射
│       ├── browserControlCommandClient.js CLI 客户端，向命令 API 发送控制请求
│       ├── cdpInputParameters.js         viewer 输入到 CDP 参数映射
│       ├── httpRoutes.js                 静态资源、命令 API 和 CDP HTTP 代理
│       ├── webSocketRoutes.js            viewer 会话、帧背压和 CDP WebSocket 代理
│       ├── protocol.js                   viewer 消息解析、校验和编码
│       └── url.js                        HTTP/HTTPS 导航地址规范化
└── tests/                                映射各模块职责的轻量单元测试和共享 fixture
```

主要调用链：

```text
package.json start
└── src/server/server.js
    ├── BrowserSession ──> Playwright/Chromium ──> CDP
    │   ├── CDP screencast frame ──> webSocketRoutes.js ──> viewer
    │   │                                             └──> remoteFrameRenderer.js ──> canvas
    │   └── Playwright state/command <── httpRoutes.js、webSocketRoutes.js 和 stdin 控制台
    ├── httpRoutes.js ──> 页面资源、命令 API、DevTools/CDP HTTP
    ├── webSocketRoutes.js ──> viewer 和 DevTools/CDP WebSocket
    └── browserControlCommandClient.js ──> POST /api/command ──> httpRoutes.js

viewer pointer move ──> latestPointerMoveScheduler.js ──> CDP
viewer pointer coordinates ──> canvasRectCache.js ──> CDP input
viewer resize ──> main.js viewport calculation ──> BrowserSession.setViewport() ──> CDP
```

Linux 容器选项只在 `browserSession.js` 添加；Windows 和 macOS 使用 Playwright 默认 Chromium 启动参数。

# 可修改内容

- 实验服务端、浏览器端和测试代码
- 实验项目自己的依赖配置和运行脚本
- 与 `uuid:12` 浏览器会话直接相关的协议实现

# 不可修改内容

- 不要接入当前仓库的 FastAPI 后端
- 不要接入 VS Code Webview 生产 UI
- 不要把实验协议视为最终产品 API

# 规范

- 固定 Playwright 浏览器 ID 为字面量 `uuid:12`
- JavaScript 使用 ESM，通过 `import` / `export` 组织代码
- 失败时暴露具体错误，不能静默降级
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
