# Node Debug hooks

## 目录用途

存放浏览器前端 Node Debug 调试工作台的 React hooks、同步通道和 mutation 状态机。

## 可修改内容

- Node Debug owner 选择、状态轮询、跨窗口同步和 mutation 并发控制。
- 与这些 hook 直接对应的单元测试。

## 不可修改内容

- 不在本目录实现 Node Debug UI JSX 组件。
- 不在本目录定义后端协议类型或复制 API 请求契约。
- 不保留根 hooks 目录中的旧路径兼容导出或 re-export wrapper。

## 规范

- 通过 `src/clients/web/src/api` 和生成的类型使用后端接口。
- hook 的副作用边界保持清晰，状态更新必须尊重后端权威状态。
- 修改后运行相关 Bun 测试、TypeScript 检查和 `bun run --cwd src/clients/web build`。
- 代码注释使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
