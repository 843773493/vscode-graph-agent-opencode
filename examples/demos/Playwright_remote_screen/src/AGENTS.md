# 目录用途

`src/` 存放 Playwright 远程屏幕实验项目的服务端和浏览器端源码。

# 可修改内容

- `server/` 下的 Node 服务、Playwright 会话和 WebSocket 协议
- `client/` 下的远程屏幕页面、输入转发和样式
- 实验内部共享的轻量源码

# 不可修改内容

- 不要在这里引入主应用生产状态管理
- 不要依赖 VS Code 扩展宿主
- 不要接入数据库、队列或云服务

# 规范

- JavaScript 使用 ESM
- 页面状态由服务端 Playwright 会话持有
- 浏览器端只保存连接和展示状态
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
