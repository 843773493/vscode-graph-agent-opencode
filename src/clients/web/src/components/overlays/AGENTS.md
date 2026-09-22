# 目录用途

存放跨运行面复用的覆盖层与对话框：锚定浮层 AnchoredOverlay、暖色确认/操作对话框 WarmActionDialog、会话命名对话框 SessionNameDialog 和 Gateway 用户访问菜单 GatewayUserAccessMenu，负责把浮层定位、Portal 渲染与确认交互封装成可复用组件。

本目录与相邻子包的分工边界：

- `components/panels/`：停靠式面板容器；本目录是浮层/对话框，非停靠面板。
- `components/composer/`、`components/agentSessions/`、`components/workspace/`、`components/chat/`：这些子包只**消费**本目录的浮层，不在此定义各自的浮层实现。
- 术语上这些组件不属于“面板”，不得并入 `components/panels/`。

# 可修改内容

- 浮层定位、Portal 挂载与关闭交互。
- 对话框的确认/取消流程与焦点管理。
- 用户访问菜单的渲染与数据接入。
- 与上述逻辑直接相关的组件测试。

# 不可修改内容

- 不定义后端协议类型的权威来源，不复制业务状态权威。
- 不吞掉交互错误；错误必须显式呈现。
- 不在本目录实现停靠面板布局或面板内部深层子组件。

# 规范

- 浮层只依赖 props 与 `hooks/` 暴露的状态，不直接伪造业务成功。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
