# 目录用途

`components/workspace/portForward/` 存放工作区端口转发的面板实现：WorkspacePortForwardPanel 负责列表、创建、停止、重连、改本地端口与改标签等交互，以及可注入的 WorkspacePortForwardApi 契约与对应测试。

本目录与相邻子包的边界：

- `components/panels/bottomPanel/`：底部面板外壳（标签栏 + 内容区），其中 PortForwardPanel 只做本目录组件的接入层，不实现端口转发业务规则。
- `components/workspace/`：工作区会话级组件的其它族（文件树、预览、编辑器）；本目录只承载端口转发这一条链路。
- `components/overlays/`：通用浮层；本目录只消费 WarmConfirmProvider 的确认流程。
- `state/display/`：展示态镜像；本目录不持有端口转发的权威状态，数据只来自 `gatewayApi`。

# 可修改内容

- 端口转发列表、创建表单、状态与错误展示及其局部交互。
- 端口号校验、标签与本地地址展示等本族纯展示逻辑。
- 与上述逻辑直接相关的组件测试。

# 不可修改内容

- 不定义后端协议类型权威（来自 `types/backend` 与 `gatewayApi`）。
- 不在本目录实现 Gateway 路由、端口转发后端规则或 SSH 隧道编排。
- 不吞掉请求或校验错误；错误必须显式呈现。

# 规范

- 端口数据只以 `gatewayApi` 返回为依据，不在本目录伪造成功状态。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 错误文案归一复用 `utils/errorMessage` 的唯一实现，不在本目录重复定义。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
