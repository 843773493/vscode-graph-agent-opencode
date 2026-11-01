# 目录用途

`src/` 是 JavaScript/TypeScript 产品源码根目录，包含客户端运行面、工作区辅助服务和跨进程共享协议。

Web 与 Electron 共用 `src/clients/workbench/` 的完整 Code OSS 工作台及 `src/clients/web/` 的 React 产品；Electron 原生宿主位于对应目录。React Native 与移动 parity 仍为 TODO，不预写未授权平台能力。

## 可修改内容

- `src/clients/workbench/` 中的原版工作台贡献及 `src/clients/web/` 中的共享 React 页面、状态和 API。
- `src/clients/shared/` 中已经证明需要复用的纯业务模型或 React DOM 组件。
- `src/workspace-services/` 中 Browser、Terminal 等辅助服务。
- `src/shared/` 中跨进程协议、传输和常量。
- 客户端实现应按运行面放入 `src/clients/` 对应目录。

## 不可修改内容

- 不要在本目录存放 Python 后端业务代码；后端位于根目录 `app/`。
- 不要在尚未启用的客户端目录预写 adapter、bridge 或平台实现。
- 不要为旧路径创建转发模块、重复源码或双路径构建。
- 不要为了未来兼容性预写尚无调用方的 adapter、bridge 或平台实现。

## 规范

- Workbench 入口组合共享 React 产品；共享产品不得反向导入工作台入口或原生宿主。
- `src/workspace-services` 可以依赖 `src/shared`，不得依赖客户端。
- JavaScript 始终使用 ESM；新增源码子目录必须包含四段式 `AGENTS.md`。
- 修改纯 Web 后运行 `bun run --cwd src/clients/web build`。
- 不要重新引入已经移除的 VS Code extension、Webview 或旧客户端测试目录。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
