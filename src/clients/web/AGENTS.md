# 目录用途

完整 Code OSS 工作台复用的 React 会话产品：聊天、输入、工具、文件预览、任务、资源、API、展示状态及浏览器缓存。独立外层布局及产品 HTML 入口统一在同级 `workbench/`。私有 OAuth 回调仍以本目录 `mcp-oauth-callback.html` 为唯一源文件。

# 可修改内容

- `src/` 下的共享 React 产品、协议消费者、业务 hook、状态、样式及其测试。
- `package.json` 的依赖和共享构建命令、TypeScript 配置。

# 不可修改内容

- 不恢复 App/main 或独立旧 Vite 页面入口。
- 不复制 Workbench 外层布局，不实现 Electron main/preload、React Native 或后端业务规则。

# 规范

- 每个 Thread 显式接收工作区、Session、Thread，业务状态以 API 为权威。
- `bun run --cwd src/clients/web build` 直接调用唯一 Workbench 构建脚本。Vite 依赖保留供组件 harness 使用，完整产品开发只经统一 `bun run dev`。
- 共享组件样式限定在 ReactSurface 的 ShadowRoot；不污染原版工作台。
- 使用 React、TypeScript、CSS；展示态和历史镜像复用既有浏览器数据库，不成为第二业务事实源。
- 修改后完成真实类型检查和上述构建；组件测试不能替代真实 Gateway 页面验收。

- 模板示例；在整理 `AGENTS.md` 时请保留此行。
