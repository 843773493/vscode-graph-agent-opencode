# 目录用途

`src/server/` 存放实验项目的 Node HTTP 服务、Playwright 会话管理、CDP screencast 和 WebSocket 协议入口。

# 可修改内容

- HTTP 静态资源服务
- WebSocket 消息校验和路由
- Playwright 浏览器生命周期、终端命令和 CDP 输入事件

# 不可修改内容

- 不要接入主应用 FastAPI 服务
- 不要增加数据库、消息队列或云服务依赖
- 不要把页面失败伪装成成功状态

# 规范

- 使用 Node.js 内置模块和明确依赖
- 对非法 browserId、非法消息和缺失字段快速失败
- `detach` 只断开 Web 查看器，不关闭 Playwright 浏览器
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
