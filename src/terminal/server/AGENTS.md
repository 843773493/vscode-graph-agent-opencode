# 目录用途

`src/terminal/server/` 存放持久终端管理后端。它负责启动和管理 PTY、维护终端元数据、通过 HTTP/JSON API 与 WebSocket 提供 attach/detach 和交互能力。

## 可修改内容

- 可以新增或修改终端 manager、HTTP 路由、WebSocket 协议处理和状态持久化逻辑。
- 可以维护测试友好的假 PTY 注入边界。
- 可以调整终端状态 DTO 字段，但要同步 Python 资源视图和前端 attach 页面。

## 不可修改内容

- 不要把主聊天前端组件放到这里。
- 不要直接依赖 FastAPI 容器或 Python 服务。
- 不要把终端状态持久化到 `.boxteam/terminal-manager/` 以外的位置。

## 规范

- 使用 Node.js ESM。
- 失败时返回明确 HTTP 错误或 WebSocket error 消息。
- detach 只断开客户端，不杀死 PTY；kill/delete 才终止终端。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
