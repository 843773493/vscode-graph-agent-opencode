# 目录用途

`Playwright_remote_screen/` 是 Playwright 远程屏幕实验项目，用于验证本地浏览器会话、终端命令控制和 Web 端 attach/detach 远程操控能力。

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
