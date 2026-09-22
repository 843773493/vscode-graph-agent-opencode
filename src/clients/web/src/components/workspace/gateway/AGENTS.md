# 目录用途

`components/workspace/gateway/` 存放 Gateway 控制面的专用组件：控制中心 GatewayControlCenter、连接对话框 GatewayConnectionDialog、入站访问面板 GatewayInboundAccessPanel、诊断面板 GatewayDiagnosticsPanel、主题设置 GatewayThemeSettings，以及工作区分组纯逻辑 gatewayWorkspacePresentation。

本目录与相邻子包的边界：

- `components/workspace/`：工作区会话级组件（文件、编辑器、预览）的平铺实现，本目录只承载 Gateway 控制面单一目标的渲染与交互。
- `components/panels/`：可停靠面板容器；本目录的组件不是停靠面板，其日志展示纯逻辑仍复用 `components/panels/gatewayLogPresentation`。
- `components/shell/`：应用外壳；本目录只消费其 WarmConfirmProvider。
- `components/overlays/`：通用浮层；GatewayConnectionDialog 是 Gateway 专属对话框，暂不并入。
- `state/display/`：展示态镜像；本目录不保存任何 Gateway 业务权威状态。

# 可修改内容

- Gateway 控制中心、连接、入站访问、诊断与主题设置的布局与交互。
- Gateway 工作区分组的纯展示逻辑与相关组件测试。

# 不可修改内容

- 不定义 Gateway 协议类型权威（来自 `types/backend` 与 `gatewayApi`）。
- 不在本目录实现 Gateway 转发、生命周期编排或 `.boxteam/` 业务数据读写。
- 不吞掉请求错误；错误必须显式呈现。

# 规范

- Gateway 数据只以 `gatewayApi` 返回为准，不在本目录伪造成功状态。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
