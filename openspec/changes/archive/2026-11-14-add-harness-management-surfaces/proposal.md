## Why

完整 Code OSS Workbench 已承载会话、目标化设置、插件和 MCP，但 Provider、Agent、Skill 等管理仍依赖配置文件或会话内选择器，连接、运行和诊断入口分散，部分按钮尚不可用。需要形成可发现、归属明确的多目标 Agent Harness 管理界面，让用户从本地目标开始管理，并复用成熟的 Workbench 机制。

## What Changes

- 固化页面能力清单、当前源码接线、业务 owner、VS Code 可复用入口和验收范围；清单不等于要求每项新建一张页面。
- 管理入口初次默认当前入口Gateway的本地目标，工作区首次默认所选 Gateway 的首个已登记项并明确展示，切回恢复该入口的上次选择；对象命令携带并捕获稳定owner，ContextKey只控制显隐／可用性。客户端、Gateway、共享来源、工作区及Thread分别表达。
- 复用原命令、菜单、Quick Pick、Settings、JSONC、EditorPane 和 ViewPane，补齐欢迎入口、目标管理、业务实体列表与详情；不建立第二套设置中心或命令注册表。
- 为Provider、Agent、工具和Skill补齐管理闭环；表单与JSONC共用原草稿，业务来源显式保存、客户端设置沿原行为。定义、工作区默认值、工具运行时覆盖与当前会话选择分别沿原owner；工具测试先明确Provider范围及费用，Skill按实际只读／可写与刷新权限接入，不新建复制机制。
- 统一发现已有插件、MCP、持续任务、终端、浏览器、端口、调试、上下文和日志入口；工作区／连接移除保留原数据及共享进程生命周期边界，接线复用已有实现。
- 将客户端缓存、配置应用状态、目标诊断、统计和存储维护按真实 owner 展示；未支持能力明确说明，不用禁用按钮或成功占位冒充完成。
- 增加少量真实 Gateway／工作区／Workbench 验收场景，约束普通交互总延迟、按需加载及多编辑器目标隔离；按纵向切片选择代表 API，不做页面与平台的全排列。无会话入口不等于 Backend 离线仍可读写配置。

## Capabilities

### New Capabilities

- `harness-management-navigation`：管理信息架构、默认本地目标、显式范围、原生入口与精确编辑器归属。
- `harness-agent-configuration-ui`：Provider、Agent、工具、Skill 与正式配置来源的管理体验和生效反馈。
- `harness-operations-management`：运行、插件及 MCP 入口整合、诊断、用量、缓存与维护的展示边界和真实验收。

### Modified Capabilities

无。本变更新增管理界面要求，引用并服从原配置、插件、搜索、任务、URI、日志、语言和缓存合同，不重新定义其业务状态机。

## Impact

- 客户端：`src/clients/workbench/src` 的管理目标、命令／菜单／视图、Settings 和自定义 EditorPane；业务组件和 hook 留在 `src/clients/web`，纯共享逻辑沿现有 shared/core。Web／Electron 共用同一 Workbench，不增加原生 IPC 或第二套聊天产品。
- 后端：优先使用现有 Gateway 控制面及 Workspace 配置／资源／运行 owner；缺失的实体操作或诊断仅在原 owner 补齐必要接口，跨进程类型沿 `proto/` 生成，不在 Gateway 新增 Agent 业务或直接读写工作区数据。
- 分工：`add-target-aware-extension-management` 拥有插件生命周期、MCP、目标化设置和秘密参数合同；`add-workbench-quick-access` 拥有命令与搜索 Provider；`refine-workbench-layered-caches` 拥有缓存；`unify-cross-end-state-sync` 拥有其实施范围内的同步。已完成能力只接入，不另建 registry、同步协议或存储。
- 不恢复 VSIX／Marketplace／用户 Extension Host、旧 Gateway 用户档案、Agent memory、工具审批中心、自动全目标搜索、自动修复／回滚或全量审计。`python_exec`、持续任务条件脚本和 ffmpeg 保持原实现。
- 前端交互切片与其余领域计划分别按 tasks 的真实验收记录交付；不以能力清单或局部通过授权发布、迁移用户数据或新增原生能力。
