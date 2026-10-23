# src/workspace-services/terminal

## 目录用途

`src/workspace-services/terminal/` 存放独立的持久终端管理进程及其浏览器 attach 页面。该目录服务于本地开发运行时，由 `scripts/launch/dev.mjs` 启动，不属于 `src/clients/web` 主前端，也不属于 Python FastAPI 后端。

```text
terminal/
├── server/      PTY、终端状态与持久化
│   ├── processes/  操作系统进程、隔离PTY宿主与退出确认
│   └── output/     输出字节边界、增量重放和逐连接流控
├── client/      attach网页及静态HTTP服务
├── protocol/    HTTP/WebSocket消息的线格式适配
└── shared/      Node与浏览器共用的无状态命令文本处理
    └── commandText.js  标记、退出码、输入标签与展示行过滤
```

先按资源生命周期和运行面分层；只有已有跨运行面消费者的纯逻辑才进入shared。命令标记过滤在后端快照和浏览器增量输出中共用实现，不能各保留一份算法。真实PTY缓冲与输出sequence仍只由server持有，派生展示文本不成为新的状态来源。

按问题定位：[终端生命周期](server/AGENTS.md)、[进程与PTY宿主](server/processes/AGENTS.md)、[输出与流控](server/output/AGENTS.md)、[attach页面](client/AGENTS.md)、[线格式](protocol/AGENTS.md)、[命令文本](shared/AGENTS.md)。

## 可修改内容

- 可以新增或修改终端后端、终端网页前端、终端协议与本地运行脚本。
- 可以维护与 `.boxteam/terminal-manager/` 状态文件相关的实现。
- 可以调整终端 attach/detach、输入输出、resize、kill 等本地终端交互逻辑。

## 不可修改内容

- 不要把 Python 业务服务或 FastAPI 路由实现放到这里。
- 不要在这里实现主聊天 UI 的资源视图；主 UI 仍由 `src/clients/web` 负责。
- 不要把终端状态写到 workspace 以外的位置。

## 规范

- 使用 ESM，通过 `import`/`export` 编写 Node.js 代码。
- 终端错误必须显式返回或抛出，不要静默失败。
- 后端默认监听 `127.0.0.1:8012`，前端默认监听 `127.0.0.1:8013`。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
