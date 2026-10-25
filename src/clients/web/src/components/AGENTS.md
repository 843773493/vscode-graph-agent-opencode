# 目录用途

存放浏览器前端的 React 展示组件，负责 UI 渲染、局部交互和组合已有状态/API 能力。

## 按职责查找

```text
components/
├── shell/               应用外壳、启动错误态、工具栏和主窗口底部面板装配
├── panels/              可停靠面板及其子树：资源树、会话面板、底部面板、只读诊断面板
├── workspace/           工作区文件树、预览、Gateway 控制面与端口转发
├── agentSessions/       Agent 会话列表和资源浏览；当前还承载 Gateway 工作区导航（待归位）
├── chat/                对话 transcript、turn 展示、滚动定位与待处理队列
├── composer/            消息输入、附件、工具选择和发送交互
├── overlays/            锚定浮层、菜单和确认/命名对话框
├── cards/               Goal 等嵌入式卡片
├── contextInspection/   冻结请求的 selection 与 history 只读检查
├── eventQueue/          诊断事件历史的分页与滚动窗口
├── nodeDebug/           Node.js 源码调试工作台与调试视图
├── WorkbenchStatusBar   主窗口状态栏展示
└── deliveryPolicyPresentation.ts 消息投递策略的纯展示文案
```

先按窗口区域定位外壳、面板、工作区内容、聊天或输入组件，再进入对应功能子树。`agentSessions/` 当前同时承载会话资源树和 Gateway 工作区导航，Gateway 导航待从该混合入口归位；Gateway 工作区导航所需数据由 Gateway API 提供，并由 App 层通过 props 传入。`workspace/gateway/` 放 Gateway 控制中心等专用面板。浮层放在 `overlays/`，不可停靠的嵌入卡片放在 `cards/`；诊断专用视图按功能放入 `contextInspection/`、`eventQueue/` 或 `nodeDebug/`。各目录的 `AGENTS.md` 负责更细的文件树与边界。

# 可修改内容

- 新增或调整页面组件、局部 UI 组件和组件私有交互逻辑。
- 将组件中过大的展示片段拆成同目录下的小组件。
- 引入来自 `state`、`utils`、`hooks` 和 `types` 的既有能力。

# 不可修改内容

- 不在本目录实现后端 API 协议、业务状态权威逻辑或持久化规则。
- 不在组件中硬编码环境变量、服务地址或后端配置。
- 不在组件中放置可复用的复杂数据转换逻辑；应下沉到 `state` 或 `utils`。

# 规范

- 组件应优先保持单一职责，避免一个文件同时承担数据聚合、请求协议和大量 UI 分支。
- 用户可见文案使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
