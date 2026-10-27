# 目录用途

`components/workspace/` 存放 Web 工作区工作台的组件：工作区辅助标签、文件与会话更改树、内容与运行时预览、Gateway 控制面板和端口转发面板。该目录既包含组合外壳，也包含按功能拆分的子目录。

## 代码树

```text
WorkspaceEditorHeader
└── WorkspaceAuxiliaryPanel                 辅助标签导航与视图组合
    ├── fileTree/                            工作区文件树、目录缓存、操作入口
    ├── sessionChanges/                      会话变更集与文件变更树
    └── 外部注入的资源、Goal、调试、附件视图

WorkspaceFilePreviewArea                    工作区文件和会话 diff 预览、编辑
├── preview/                                 Markdown、图片、附件、Mermaid 渲染器
└── NodeDebugBreakpointGutter                预览中的 Node 调试断点操作
WorkspaceRuntimePreviewArea                 终端与浏览器运行时预览
WorkspaceFileReferenceContext               文件资源引用上下文

gateway/                                    Gateway 控制中心、连接、诊断、主题
portForward/                                工作区端口转发面板与端口校验
mcp/                                        工作区 MCP 连接管理与会话授权入口
WorkspaceAddDialog.tsx                      添加对话框轻量入口与延迟加载
└── workspaceAddDialog/                     设备选项模型与目录选择交互
WorkspaceRenameDialog                       工作区重命名对话框
```

子目录分别用自己的 `AGENTS.md` 说明细节；维护时先按上面的职责树定位 owner，再遵守对应子目录规范。`gateway/` 展示 Gateway 控制面数据，不因此变成工作区业务状态的 owner。

# 可修改内容

- 顶层辅助标签、编辑器标题和工作区组合视图。
- 顶层文件资源引用和预览接线，以及工作区管理对话框。
- `fileTree/`、`sessionChanges/`、`preview/`、`gateway/`、`portForward/` 下的细节按各自 `AGENTS.md` 修改。

# 不可修改内容

- 不放入会话消息流、Agent State、请求日志等无关组件。
- 不在组件里实现后端业务规则、Gateway 路由或工作区文件系统规则。
- 不在顶层重复实现子目录已有的文件树、预览、Gateway 或端口转发职责。

# 规范

- 组合组件通过 props 接收权威状态与动作；请求和业务状态仍由对应 API、hook 或 state owner 管理。
- 错误必须清楚呈现，提示不得导致周边面板大面积移动。
- 子目录的专用约束以其 `AGENTS.md` 为准；协议与业务规则不在展示组件内复制。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
