# 目录用途

`src/` 是 JavaScript/TypeScript 产品源码根目录，包含当前维护的纯 Web 客户端、工作区辅助服务、跨进程共享协议，以及暂时保留在旧位置的 VS Code 扩展实现。

当前开发范围只有 `src/clients/web/`。Electron、React Native 和新的 VS Code 客户端仅在 `src/clients/` 下预留 TODO 边界；不得把“预留目录”理解为需要同步实现多端功能。

## 可修改内容

- `src/clients/web/` 中的纯 Web 页面、交互、状态和 API 适配。
- `src/clients/shared/` 中已经证明需要复用的纯业务模型或 React DOM 组件。
- `src/workspace-services/` 中 Browser、Terminal 等辅助服务。
- `src/shared/` 中跨进程协议、传输和常量。
- 用户明确指定时，才可修改现存 VS Code 扩展相关目录。

## 不可修改内容

- 不要在本目录存放 Python 后端业务代码；后端位于根目录 `app/`。
- 不要在本阶段实现 Electron、React Native 或新的 VS Code 客户端。
- 不要为旧路径创建转发模块、重复源码或双路径构建。
- 不要为了未来兼容性预写尚无调用方的 adapter、bridge 或平台实现。

## 规范

- 依赖方向为 `src/shared` → `src/clients/shared/core` → `src/clients/shared/web-ui` → `src/clients/web`。
- `src/workspace-services` 可以依赖 `src/shared`，不得依赖客户端。
- JavaScript 始终使用 ESM；新增源码子目录必须包含四段式 `AGENTS.md`。
- 修改纯 Web 后运行 `bun run --cwd src/clients/web build`。
- 现存 `extension.js`、`backend/`、`webview/`、`webview-ui/`、`test/` 是阶段性保留的旧 VS Code 实现，后续迁移必须使用独立 OpenSpec。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
