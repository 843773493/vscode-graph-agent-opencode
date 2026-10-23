# 目录用途

存放浏览器前端 Gateway 工作区域的数据层 hook：工作区激活切换、父子层级调整、增删改变更和运行时生命周期编排。

## 职责树与定位

```text
gatewayWorkspace/
├── activeGatewayWorkspaceFields.ts           当前激活工作区的派生字段
├── useGatewayWorkspaceActivation.ts          激活切换、连接与失败回滚
├── useGatewayWorkspaceHierarchy.ts            Gateway 工作区父子层级
├── useGatewayWorkspaceMutations.ts             新建、重命名、删除与状态收敛
├── useGatewayWorkspaceRuntimeLifecycle.ts      工作区进程与预览的运行生命周期
├── useManagedBackendConfirmation.ts            整组 Backend 操作确认与成员合同
└── *.test.tsx                                   对应 hook 的行为和竞态测试
```

按状态归属定位：改变当前选择及切换结果进入 `useGatewayWorkspaceActivation.ts`；改变 Gateway 导航树进入 `useGatewayWorkspaceHierarchy.ts`；改变工作区记录进入 `useGatewayWorkspaceMutations.ts`；改变托管 Backend 启停、重启及其完整成员确认进入 `useGatewayWorkspaceRuntimeLifecycle.ts` 或 `useManagedBackendConfirmation.ts`，前者负责运行期状态接线，后者负责用户确认与请求合同。纯粹从已获取的 Gateway 列表提取当前工作区字段时使用 `activeGatewayWorkspaceFields.ts`，不要把 JSX 展示和协议类型定义放进 hook 目录。

# 可修改内容

- Gateway 工作区激活、连接建立与失败回滚流程。
- 工作区父子层级、重命名和删除等变更请求及其后的状态收敛。
- 工作区运行时（进程/预览）生命周期副作用。
- 与上述逻辑直接相关的 hook 测试。

# 不可修改内容

- 不实现 UI JSX 组件。
- 不定义后端协议类型的权威来源，协议类型仍取自 `types/backend` 和 `types/frontend`。
- 不吞掉 Gateway 或后端错误；错误必须写入状态或继续抛出。

# 规范

- 业务状态只以后端返回的完整对象为依据，成功后整体替换、失败后主动重取。
- 工作区级共享类型（如 `SetAppState`、`FinishWorkspaceRefresh`）统一从 `../contentViewLoaderTypes` 取得，不在本目录另建收窄副本。
- 竞态和乱序响应必须显式丢弃，不得静默跳过。
- 代码注释使用中文，专业术语除外。

模板示例；在整理 `AGENTS.md` 时请保留此行。
