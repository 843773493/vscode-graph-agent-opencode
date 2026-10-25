# 目录用途

为会话 hook、会话资源面板及其跨目录消费者提供测试装具。按依赖和被测 hook 分拆，避免单个 fixture 模块静态加载无关业务 hook。

```text
session/
├── gatewayEnvironment.ts        Gateway fetch、路由和响应 fixture
├── gatewayBrowserGlobals.ts     按需安装 window/document 桩
├── asyncControl.ts              effect tick 与可显式释放的并发门闩
├── appState.ts                  AppState 构造与状态镜像
├── sessionCatalogOutbox.ts      会话目录 outbox 状态机测试运行时
├── reactHarness.ts              共享 React hook 挂载器
├── sessionLifecycleActions.ts   生命周期动作 hook harness
├── sessionRunActions.ts         运行动作 hook harness
├── sessionMessageStream.ts      消息流 hook Harness 工厂
├── sessionResourceExplorer.ts   资源探索器状态、props 和 harness
├── sessionGoalController.ts     Goal 控制器 harness
└── sessionViewState.ts          会话视图状态 harness
```

# 可修改内容

- 可以维护上表中测试装具的类型、数据工厂和 hook 挂载代码。
- 可以在所有调用方一并迁移时重命名或拆分 fixture API。

# 不可修改内容

- 不实现产品 API、业务状态或替代生产 hook 的行为。
- 不把互不相关的 hook 静态导入同一个 fixture 模块。
- 不返回未声明请求的虚假成功响应，也不吞掉测试错误。

# 规范

- 测试按需导入单个模块，不新增统一转发入口。
- 生产状态转换复用对应纯状态机；fixture 只负责依赖装配和测试输入。
- 浏览器全局桩必须显式安装并在每个用例后还原。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
