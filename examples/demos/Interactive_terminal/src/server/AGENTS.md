# 目录用途

`src/server/` 存放原型 Node HTTP 服务、WebSocket 协议入口和 `node-pty` 终端管理器。

```text
server/
├── protocol.js        校验浏览器发送的终端消息
├── server.js          HTTP/WebSocket 入口、终端输出 JSON 缓存、背压和服务关闭
└── terminalManager.js 管理共享 PTY 会话、统一写入和成功提交终端尺寸
```

# 可修改内容

- HTTP 静态资源服务
- WebSocket 消息校验和路由
- `TerminalManager` 与 PTY 生命周期管理

# 不可修改内容

- 不要接入主应用 FastAPI 服务
- 不要增加数据库、队列或云服务依赖

# 规范

- 使用 Node.js 内置模块和明确依赖
- 对非法 terminalId、非法消息和缺失字段快速失败
- `detach` 只断开客户端，不终止 PTY
- `attach` 先完成 PTY resize 再登记客户端；resize 失败时不得更新尺寸或留下客户端映射
- 服务收到 `SIGINT` 或 `SIGTERM` 时先停止接受 HTTP/WebSocket 新连接，再终止所有 PTY；WebSocket 发送关闭帧并等待最多 1 秒，普通 HTTP keep-alive 和残留 WebSocket 分别在宽限期后关闭
- PTY 退出时先发送 `exit` 再关闭该客户端连接；清理按 `clientId` 对原 PTY session 执行，不能误清理后续新建的同 ID session
- 同一 PTY 输出事件使用不可变共享对象和一份 JSON 编码结果，再发送给所有已连接客户端
- 每个 WebSocket 发送缓冲最多 8 MiB；慢客户端因终端输出过载关闭时，页面必须显示明确原因
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
