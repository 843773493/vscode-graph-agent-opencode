# 目录用途

存放工作区层级的 React hook，以及紧邻这些 hook 的请求快照、观察生命周期和资源动作协调函数。

## 职责树

```text
workspace/  (所有文件直接位于本目录；测试与被测实现按缩进关联)
├── useWorkspaceBootstrap.ts                   React 引导状态、刷新代际与生命周期
│   └── useWorkspaceBootstrap.regression.test.tsx  引导状态、并发和失效竞态
├── workspaceBootstrapLoader.ts                 Gateway/Workspace 数据加载与有界重试
│   └── workspaceBootstrapLoader.test.ts         可取消的重试等待
├── workspaceBootstrapPolicy.ts                 工作区、会话和用户视图状态选择规则
│   └── workspaceBootstrapPolicy.test.ts         选择优先级和重试错误判定
├── workspaceSessionListRefresh.ts              会话列表快照去重、失效代际与强制刷新
│   └── workspaceSessionListRefresh.test.ts     快照合并和失效边界
├── useWorkspaceRefreshOrchestration.ts         页面层工作区刷新接线
│   └── useWorkspaceRefreshOrchestration.test.tsx
├── useWorkspaceSessionSelection.ts              工作区会话切换队列
│   └── useWorkspaceSessionSelection.test.tsx
├── useWorkspaceSessionActivity.ts               工作区范围的会话后台活动流
│   └── useWorkspaceSessionActivity.test.tsx
├── workspaceObservationLifecycle.ts             跨工作区观察注册、暂停与恢复
│   └── workspaceObservationLifecycle.test.tsx
├── useWorkspaceFileWatch.ts                      文件变更观察与重连
│   ├── useWorkspaceFileWatch.test.tsx
│   └── useWorkspaceFileWatch.reconnectStatus.test.tsx
├── useWorkspacePreviewTabs.ts                   预览标签、文件内容和布局持久化
│   └── useWorkspacePreviewTabs.test.tsx
├── useWorkspaceTerminalResources.ts              工作区终端资源查询与控制
│   └── useWorkspaceTerminalResources.test.tsx
├── createTerminalFromResourcePanel.ts             资源面板创建终端的跨 hook 动作编排
│   └── createTerminalFromResourcePanel.test.tsx
└── useWorkspaceInformationClipboard.ts            工作区信息复制
```

本目录有 13 个实现模块和 13 个相邻测试文件；总文件数超过 20 主要来自就近测试。工作区领域本身仍是清晰的检索边界，只有某条职责扩展为多模块并形成独立 owner 时才继续下沉子目录，并为该目录补充 `AGENTS.md`。树中的缩进表示测试关联，不表示物理子目录。

`workspaceObservationLifecycle.ts` 还由 session 与 Gateway workspace hook 直接消费；调整它的位置前先迁移这些跨目录调用方。`workspaceSessionListRefresh.ts` 是引导和 session event stream 共用的请求快照 owner。新增长期业务状态或通用变换应放到 `state/` 的权威模块，不要继续塞进工作区 hook 目录。

# 可修改内容

- 工作区启动 hook 的状态提交与刷新协调；数据加载位于 `workspaceBootstrapLoader.ts`，纯选择规则位于 `workspaceBootstrapPolicy.ts`。
- 工作区会话列表快照的拉取、代际校验与强制刷新。
- 工作区会话选择队列、未读活动标记与后台活动流。
- 工作区预览标签页的状态与布局持久化、工作区文件变更监听和信息剪贴板。
- 与上述逻辑直接相关的纯函数测试。

# 不可修改内容

- 不定义后端协议类型的权威来源，不实现 UI JSX 组件。
- 不吞掉后端错误；错误必须写入状态或继续抛出。
- 不在本目录伪造工作区业务状态，成功状态只来自后端返回值。

# 规范

- hook 围绕清晰的工作区副作用边界命名，复杂状态变换优先复用 `state/` 下的纯函数。
- 导入其他 hook 或共享模块时使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
