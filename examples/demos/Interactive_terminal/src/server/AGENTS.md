# 目录用途

`src/server/` 存放原型 Node HTTP 服务、WebSocket 协议入口和 `node-pty` 终端管理器。

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
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
