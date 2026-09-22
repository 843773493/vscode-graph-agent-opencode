# 目录用途

存放应用外壳层组件：顶层 Provider（WarmConfirmProvider）、错误边界（AppErrorBoundary）、启动骨架（BootstrapState）与主工具栏（Toolbar），负责应用启动、错误兜底和全局工具栏交互。

本目录与相邻子包的分工边界：

- `components/overlays/`：浮层与对话框；本目录的 Toolbar / WarmConfirmProvider 只消费它们。
- `components/panels/`、`components/cards/`、`components/workspace/` 等：业务面板与卡片；本目录是承载它们的应用外壳，不含业务渲染。
- 术语上这些组件不属于“面板”“卡片”或“浮层”，不得并入上述子包。

# 可修改内容

- 应用级 Provider 的组合与上下文暴露。
- 错误边界的兜底渲染与恢复入口。
- 启动骨架与加载态展示。
- 主工具栏的视图切换与全局动作入口。
- 与上述逻辑直接相关的组件测试。

# 不可修改内容

- 不定义后端协议类型的权威来源，不复制业务状态权威。
- 不吞掉渲染或交互错误；错误必须显式呈现。
- 不在本目录实现业务面板、卡片或浮层内部实现。

# 规范

- 外壳组件只依赖 `hooks/` 暴露的状态与 props，不伪造业务成功。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
