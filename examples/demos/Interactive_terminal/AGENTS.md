# 目录用途

`Interactive_terminal/` 是交互式终端最小原型，用于验证 `@xterm/xterm`、WebSocket 和 `node-pty` 串联后的用户与 agent 双向操控能力。

```text
Interactive_terminal/
├── package.json             依赖、Node 启动与 Bun 检查/测试命令
├── bun.lock                 demo 依赖锁定
├── scripts/check.mjs        服务端与浏览器入口的语法检查
├── src/
│   ├── client/
│   │   ├── index.html       终端页面结构
│   │   ├── main.js          WebSocket、xterm 输入输出和连接状态
│   │   └── style.css        页面布局
│   └── server/
│       ├── server.js        HTTP/WebSocket 入口、广播和服务关闭
│       ├── protocol.js      客户端与服务端消息解析、校验和编码
│       └── terminalManager.js shell 选择、PTY 生命周期、客户端扇出
└── tests/                   浏览器连接状态、fake PTY 与协议单元测试
```

主要调用链：

```text
package.json start
└── src/server/server.js
    ├── HTTP ──> src/client/* 静态页面
    └── WebSocket
        ├── protocol.js 校验 attach/input/resize/detach
        └── terminalManager.js ──> node-pty
            ├── 浏览器输入 ──> PTY write/resize
            └── PTY 输出 ──> 共享编码事件 ──> 所有已 attach 客户端

SIGINT/SIGTERM ──> 关闭连接 ──> TerminalManager.killAll()
```

默认 shell 在 Windows 使用 `COMSPEC`/`cmd.exe`，在 Linux 和 macOS 使用 `SHELL`/`/bin/sh`。

# 可修改内容

- 原型服务端、浏览器端和测试代码
- 原型的 `package.json`、`bun.lock` 和运行脚本
- 与终端 WebSocket 协议直接相关的实验实现

# 不可修改内容

- 不要接入当前仓库的 FastAPI 后端
- 不要接入 VS Code Webview 生产 UI
- 不要把实验协议视为最终产品 API

# 规范

- 固定终端 ID 为字面量 `uuid:12`
- Web 进程由 Node 运行，依赖安装和脚本入口使用 `bun`
- 错误要快速失败，不能静默降级
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
