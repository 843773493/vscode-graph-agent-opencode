# 目录用途

`components/panels/resourceTree/` 存放可重连资源树族：会话内资源面板 ResourcePanel、扩展窗口跨 Gateway 资源面板 GatewayExtensionResourcePanel、二者共用的资源树行 ResourceTreeRow，以及 ResourcePanel 的渲染测试。它们把 `state/display/resourceDisplay` 的资源展示投影渲染成树形面板。

本目录与相邻子包的边界：

- `components/panels/`：其它可停靠面板容器（对话、事件、请求、底部面板族、Agent 状态等）；本目录只承载资源树这一条链路。
- `components/workspace/`：工作区文件、端口与 Gateway 工作区面板；本目录不渲染工作区文件树，只渲染会话/Gateway 层可重连资源。
- `components/chat/`：对话回合渲染单元；与本目录无耦合。
- `components/shell/`：应用外壳与 WarmConfirmProvider 等提供者；本目录只消费，不实现。
- `state/display/`：资源展示投影的权威纯逻辑；本目录不复制其状态映射、图标映射或文案映射。

# 可修改内容

- 资源树的分组渲染、折叠、选中态与行内操作按钮布局。
- 资源操作确认文案、提示条与复制交互的前端编排。
- 与上述逻辑直接相关的组件测试。

# 不可修改内容

- 不定义资源协议类型权威（来自 `types/backend`）。
- 不在本目录复制资源状态/图标/文案映射，必须复用 `state/display/resourceDisplay`。
- 不发起 HTTP 请求或持有资源业务状态权威；资源数据只由 App 层通过 props 传入。

# 规范

- 资源数据只以 App 层传入的 props 与事件流为依据，不在本目录伪造成功状态。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
