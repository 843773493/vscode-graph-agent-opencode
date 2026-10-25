# 目录用途

存放工作区添加对话框的按需实现：对话框关闭时不进入首屏代码，打开后再加载设备/目录选择交互。

```text
workspaceAddDialog/
├── Content.tsx        设备选择、目录浏览与添加交互
└── gatewayChoices.ts  从 Gateway 工作区状态派生设备选项
```

# 可修改内容

- 可以维护对话框打开后的交互和目录列表展示。
- 可以维护只读的 Gateway 设备选项派生逻辑及其单元测试。

# 不可修改内容

- 不在这里定义 Gateway API 或工作区状态的权威数据。
- 不复制 `WorkspaceAddDialog.tsx` 的公共 props 契约，也不保留旧实现转发层。

# 规范

- `WorkspaceAddDialog.tsx` 是轻量静态入口；仅当 `open` 为真时加载 `Content.tsx`。
- `gatewayChoices.ts` 只做输入到设备选项的纯转换，不发请求、不持有可变 UI 状态。
- 对话框加载和错误反馈必须留在浮层区域，不推动工作台布局。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
