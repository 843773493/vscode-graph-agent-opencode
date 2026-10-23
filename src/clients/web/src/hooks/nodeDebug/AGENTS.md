# Node Debug hooks

## 目录用途

存放浏览器前端 Node Debug 调试工作台的 React hooks、同步通道和 mutation 状态机。

## 职责树

```text
nodeDebug/
├── nodeDebugMutationGate.ts       按 owner 串行化调试 mutation
├── nodeDebugSync.ts               跨窗口调试状态广播通道
├── useNodeDebugOwner.ts           当前会话与线程 owner 选择
├── useNodeDebugStateSync.ts       后端状态轮询、刷新和 mutation 结算
├── useNodeDebugController.ts      调试命令与生命周期动作控制器
├── useNodeDebugWorkbench.ts       组合 owner、状态同步与工作台动作
└── nodeDebugTestFixtures.ts       单测的浏览器全局 fixture，不供生产代码导入
```

带 `.test.ts` / `.test.tsx` 的模块就近验证对应状态机或 hook；跨窗口传输与状态轮询分开，工作台 hook 只负责组合既有能力。

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
