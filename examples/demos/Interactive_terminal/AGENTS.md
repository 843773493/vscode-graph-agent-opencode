# 目录用途

`Interactive_terminal/` 是交互式终端最小原型，用于验证 `@xterm/xterm`、WebSocket 和 `node-pty` 串联后的用户与 agent 双向操控能力。

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
