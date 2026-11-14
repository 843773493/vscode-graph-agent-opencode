## Context

动机见 proposal.md。2026-11-13 的源码盘点只证明注册、导入和消费者存在，不代表本次完成真实浏览器验收；现有接线应复用，不能根据“设置还是 demo”的概括推倒重写。

关键证据（均相对仓库根）：

- `src/clients/workbench/src/managementTarget.ts`：settings／extensions／mcp 已有各自展示选择，初次默认入口 Gateway 的 local target。
- `src/clients/workbench/src/settings/{editor,sourceEditor,sourceOwner,commands}.ts` 及 `scripts/settingsEditorTransform.mjs`：原 Settings 编辑器、正式来源选择、schema 投影、同一 JSONC 文本模型和 source writer 已接线。
- `src/clients/workbench/src/react/navigationView.tsx` 实际挂载 `GatewayControlCenter`；后者已有连接、运行和诊断，但“连接与凭据”导航仍是禁用占位。
- `src/clients/web/src/components/composer/Composer{Agent,Model,Tool}Control.tsx`：已有选择、默认值和工具测试，不能当作 Provider／Agent 完整实体管理。
- `src/clients/workbench/src/extensions/servicePluginManagement.ts`：自有包、插件和实例管理；`mcp/service.ts` 和 `agent.contribution.ts` 连接原 MCP 服务／视图。
- `agent.contribution.ts`、`react/workspacePanelView.tsx`：Terminal／Output 原容器内的工作区内容仍为 React 产品，不是已经迁成原生 Terminal／Output 模型。
- `debug/contribution.ts` 接入实际调试贡献和 DAP；`automation/` 已有独立任务编辑器；`threadCacheActions.ts` 已有精确 Thread 缓存清理。
- `configs/workspace_inline.jsonc` 仍标注部分字段“未接入”；schema 能表达字段不能证明运行时消费。
- `configs/workspace_schema.jsonc` 的 `api_key` 已接受字面量／环境引用；`app/services/infrastructure/config/source_document.py` 与 `config_service/config_source_edit.py` 已提供来源脱敏、同身份秘密占位恢复和原 source writer；Provider 输入无需另造秘密存储。
- `configs/runtime.py:merge_json_objects`：对象递归合并、数组整体替换，没有删除继承项的 tombstone。`config/session_defaults.py` 与 `app/api/agents.py` 持有独立的工作区会话默认值；配置定义、会话默认值和当前 Session 选择不是同一次写入。
- `tool_selection_store.py`／`tool_service.py`：工具选择是工作区内按 Agent 的运行时覆盖；selection PATCH 仅回 changed 项。`app/tool_testing/service.py` 中空 `provider_ids` 表示该 Agent 全部 Provider，不是免费连通性检查。
- `app/gateway/routes/skill_sources.py` 的 Gateway refresh 只支持原本机管理；远端联邦提供只读投影。`app/api/resources.py` 的 Workspace 来源 refresh 是另一 owner 的操作；包 Skill 没有本变更可直接复用的专用副本创建流程。

参考 VS Code 的现有源码：`reference_repo/vscode/src/vs/workbench/contrib/preferences/browser/{settingsEditor2,keybindingsEditor}.ts`、`extensions/browser/extensionsViewlet.ts`、`remote/browser/remoteExplorer.ts`、`mcp/browser/mcpServerEditor.ts`、`debug/browser/debugViewlet.ts`、`output/browser/outputView.ts`、`markers/browser/markersView.ts`。这些只用于对照，产品构建继续使用固定 upstream；不修改或测试 reference_repo。

## Goals / Non-Goals

**Goals:** 用户能在没有 Session 的情况下配置必要对象；目标和来源持续可见；业务管理使用原注册入口；已有产品功能只补接线；每条新增管理链可独立验收。

**Non-Goals:** 不新建全局管理平台、业务对象 registry、全局索引或插件 SDK；不重做会话、插件监督、MCP、搜索和缓存；不创建用户体系、云同步或系统权限功能；不恢复 Agent memory；不迁移 `python_exec`、条件脚本和 ffmpeg。交付范围按 tasks 的实际验收记录，不将未完成规划视为已实施，也不据此改动用户环境。

## Decisions

### D1. 新建横向管理变更，领域合同仍归原 owner

新增三个界面 capability；原配置、URI、插件、搜索、任务、日志、语言等现行规范及活跃变更不复制成第二份合同。选择独立变更，是因为页面组织跨越多个领域，塞进插件或搜索变更会让它们承担无关功能。

| 关联领域 | 本变更承担 | 保留的权威合同 |
|---|---|---|
| 设置、插件、MCP | 导航、实体详情、目标展示和已有操作可达 | `add-target-aware-extension-management` 的配置来源／秘密／实例生命周期 |
| 命令、Quick Access、正文检索 | 注册管理命令并链接已有 Provider | `add-workbench-quick-access` 的模式、范围、查询、定位和取消 |
| 缓存、编辑器恢复 | 提供已有缓存信息与显式清理入口 | `refine-workbench-layered-caches` 及现行历史缓存合同 |
| 状态同步 | 消费现有回执／事件，缺失小对象仍走原读写 | `unify-cross-end-state-sync` 已落地的合同；不等待其全量完成或预写兼容层 |
| 生命周期、自动化、资源 | 入口归类、详情跳转与错误定位 | 原 Gateway／Backend／任务／资源 owner；不新增执行队列 |
| 语言 | 新文本进入现有翻译资源 | `workbench-localization`，默认中文、内置英文、切换重载 |

关联能力已经完成时接入消费者即可；尚未落地的必要接口在所属 owner 纵向补齐并由对应变更承接，不新建通用桥接服务。领域规范与源码冲突时先核对当前用户决定，不能用旧文档恢复退役链路。

### D2. 一个目标选择习惯，五种业务归属

管理入口以当前 `managementTarget` 扩展自身 scope，初次默认入口本地，显式切换只改变该入口的展示选择，不改变导航激活工作区、窗口文件根、其他管理页或已打开编辑器。浏览器重启后全局入口重新本地；编辑器恢复保存稳定 owner 并重新解析当前合法路由，不持久化过期凭据或以默认本地替代恢复失败。

Gateway 选择还不是具体工作区。Provider／Agent／工具等需要 Workspace owner 的全局管理命令，沿该入口选择的 Gateway（初次本地）展示其内部工作区；首次默认首个已登记工作区，切回 Gateway 恢复本入口上次工作区，再按需选择正式来源；不借焦点 Thread 或导航 activeId 补齐工作区，不自动创建／激活工作区。没有候选或目标不可用时显示原状态和已有创建／注册／启动入口。对象上下文已传入精确 owner 时直接使用，不重新询问或更改全局选择。

| 范围 | UI 需展示 | 权威写入及影响 |
|---|---|---|
| 客户端／窗口 | 客户端设置与当前配置档案 | 原 Workbench 配置和展示存储；无运行目标 |
| Gateway | 本地／所选远端 Gateway | 原 Gateway 配置或控制面 owner，不能读工作区数据 |
| Workspace 用户来源 | 所属环境的正式来源及共享说明 | 同一 BOXTEAM_HOME 下共享 source，按原 fan-out 影响其消费者；界面仅汇总 owner 已观察到的消费者，不归每 Backend 私有 |
| 指定工作区 | Gateway＋workspace 稳定身份和具体覆盖层 | Workspace 配置或业务 owner，不依赖后端“当前激活”隐式寻址 |
| Session／Thread | Gateway＋workspace＋session＋thread | 对象原 owner；会话选择、当前运行不等于默认配置 |

普通管理布局示意：`运行目标：本地 ▾ → 范围：Gateway／Workspace 用户来源／具体工作区 → 来源层`。后端共享进程身份只用于原生命周期管理，不作为新配置层或资源身份。共享用户来源即使经某工作区访问也不得显示为该工作区私有。fan-out 的消费者汇总是来源 owner 已观察到的范围，不是管理列表、全机器或全部 Gateway 的完整影响清单；未知／停机消费者不报告已应用，按原合同说明下次启动追赶。停机／无权目标只展示原 owner 允许取得的状态、错误和显式启动入口，不承诺 Backend 关闭时仍可读其配置，也不让 Gateway 代读文件。

选择稳定对象身份；显示名、端口、本机路径不能参与目标判定。显式打开上下文对象允许远端目标，不先改全局选择。详情输入按目标和对象稳定身份区分；同一设置对象的表单／JSON 草稿继续使用原模型，不另建 localStorage 副本。目标切换打开不同输入，原脏输入不重绑；原 dirty 流程处理关闭与重载。

配置草稿沿原 `(gateway_id, workspace_id 或 Gateway 范围, source_key)` 编辑器身份，Provider／Agent 详情只在此来源模型中定位稳定实体 ID，不为每个表单建立另一草稿。共享用户来源从两个工作区访问时仍是两个应用上下文，各自保留原基线、草稿和回执；一个保存后另一个按原文件 CAS 冲突，不按物理路径合并模型、不自动重基或同步草稿。连接 ID 是传输路由而非业务身份；操作捕获发起时路由及授权，显式重选只能影响后续操作，不能把在途提交改投新连接。配置读取与写入继续分别经过原 Gateway `configuration_read`／`configuration_write` 准入及目标 writer，不另建权限矩阵。

欢迎页的 Provider 入口在没有 Session 时仍可见；Workspace owner 已就绪后才进入实体读写。Backend 未就绪时显示真实启动／连接错误与原显式操作；配置错误阻断初始化时引导已有配置工具，不放松启动校验、不创造配置专用 Backend 或让 Gateway 代读业务文件。即便一个 Provider 缺少环境凭据，已就绪 owner 的管理定义仍从原脱敏来源及 schema 读取，不能依赖会展开全部凭据的运行时 Agent 列表而使所有定义无法编辑；配置可读不等于模型可运行。

### D3. 四个入口层次，三种主要页面形态

- 管理菜单与欢迎页：客户端设置、主题、语言、快捷键，以及目标／Provider／Agent 等可发现的管理命令。
- 左侧侧边栏：目标、连接和工作区导航；业务配置树可作为进入详情的入口，不为每种实体增加一个活动栏图标。
- 主编辑区：复杂管理对象用原 EditorPane 托管可复用 React 列表／详情；参数用原 Settings，源文件用原 JSONC。复用原 Tab、Grid、快捷键、焦点、dirty 和恢复机制。
- 底部面板与 Thread 内资源：保持既有工作区／会话归属。全局辅助侧边栏不设计新的 Agent 产品。

具体组合沿当前 Explorer 的 `SessionNavigation`，不另建一个管理工作区树：现有 Gateway／连接分组和工作区节点继续包住其会话树，在原分组／工作区节点的标题与上下文菜单增加“管理目标／连接”“工作区设置”“运行状态”等原命令。入口 Gateway 以明确本地节点提供管理入口，无会话也可进入；实体详情在主编辑区打开。复用 `workspaceContext` 与现有导航 runtime 的工作区身份、激活及会话 catalog，不在管理组件保存另一份 activeWorkspace。管理页自己的目标选择不激活导航；只有用户执行原工作区激活动作才更新原会话树及底部面板。原控制中心中的有效表单／诊断内容按实体接入详情，迁齐调用方后删除被替换对话框／禁用占位，不并存两套管理导航。

本地 Gateway 节点仅是入口 Gateway 稳定身份的导航呈现，不创建持久工作区或 catalog 节点，不参与工作区激活，不为显示无会话入口注册虚假 Backend／Workspace。

选择这套方式避免一个巨大自制“设置中心”重复菜单、键位和编辑器；也避免为表单里的每个字段新建页面。已有插件详情不强制为复用而再写一份原 ExtensionsEditor，已有 React 组件不为外观统一改写成手写 DOM。页面能力可以共享一个详情 Tab 或设置分类。

可从多入口触发的管理动作复用同一原命令，树、按钮、上下文菜单和欢迎页调用它；表单字段编辑直接使用原模型，不为每次输入注册命令或增加转发层。ContextKey 只控制显隐／可用性，沿原视图或编辑器作用域，不承载权威 owner 或代替服务端准入。对象入口显式传稳定 Gateway／Workspace，必要时带来源、实体 ID 或 Session／Thread；命令在首个 await 前捕获参数及路由，后续不读焦点猜目标。命令面板等无参数入口走 D2 的选择流程，直接命令调用也要核必要参数，不因菜单隐藏就省略。只有确需引用／重新打开的资源才登记正式 URI，不为一次性动作发明 scheme；文件打开、网络与 Gateway 链路继续复用 Web。

### D4. 完整页面能力矩阵

“已有”仅是本轮源码接线证据；“补齐”是本变更实施验收项；“受限”必须提供准确说明和已有操作，不要求创造新业务能力。矩阵不表示实施顺序，不要求每行一个独立 EditorPane。明确承诺补齐的 Provider／Agent CRUD、Skill 来源管理等，缺必要接口时仍须在原 owner 补齐并独立验收，不能以“暂不支持”勾完；统计聚合、系统能力和无既有合同的破坏性维护等受限项则以准确披露边界完成，不借矩阵扩大成新服务／存储。新增写入合同须有消费方和定向场景，不作为所有页面接入的共同前置。

源码简写 `WB/`＝`src/clients/workbench/`、`Web/`＝`src/clients/web/src/`；`Gateway…Panel` 均在 `Web/components/workspace/gateway/`。证据简写 `GM/`＝`out/tests/temp/2026/11/13/gateway_management/artifacts/`；`test_x/run-y`＝`out/tests/integration/clients/web/test_x/runs/run-y/`，`e2e:test_x/run-y` 的根为 `out/tests/e2e/clients/web/`。Quick索引6指已有 `out/tests/temp/2026/11/13/quick_access_final_audit/artifacts/scenario-evidence-6.json`，其具名 evidence entry 解析到带 SHA 的原正式结果及命令收据，不新增第二份证据 registry。每行的“实测”仅适用于所列 run 的源码／产物和运行面；纯源码核查、领域复用与未验范围分别写明。

| ID | 页面能力及主要操作 | 默认目标／范围 | 权威 owner／实际注册与消费者 | 已核证据／交付限制 |
|---|---|---|---|---|
| C01 | 欢迎：最近资源、创建会话、配置模型、连接故障入口 | 管理先本地；最近资源保留原 owner | 原 HistoryService／Session owner；`WB/scripts/harnessWelcomeTransform.mjs` 的三个 Welcome entry 直达 Provider／Agent／Gateway 原命令 | `test_harness_management_ui/run-325671635b33` 实点三入口，无 Session／激活变化；最近资源沿 Quick 既有 owner，不联机遍历全部远端。 |
| C02 | 客户端设置、聊天展示与阅读偏好 | 客户端 | 原 Workbench `IConfigurationService`／展示偏好 owner；`workbench.action.openSettings`，`Web/hooks/settings/useUiSettingsController.ts` 消费原偏好 | `test_harness_management_ui/run-325671635b33` 与 `GM/appearance-product-05.json` 验入口、搜索与实际展示项；不称逐个偏好字段均已测得。 |
| C03 | 主题、图标、字体、背景、缩放 | 客户端；背景素材沿原 Gateway 图片 owner | 原主题／字体／布局 owner；`WB/src/theme.ts` 注册 `boxteam.appearance.open` 至 F1／管理菜单／Preferences，背景沿原素材 owner | `GM/appearance-product-05.json`、`GM/settings-grid-appearance-05.json` 验分类、键盘入口和中英文；各自固定构建边界保留。 |
| C04 | 快捷键与冲突查找 | 客户端 | 原 Keybinding registry；`workbench.action.openGlobalKeybindings`，业务 Action2 沿各模块注册 | `test_harness_management_ui/run-325671635b33` 实际打开 `.keybindings-editor`；未对每项用户键位冲突作穷举。 |
| C05 | 显示语言 | 客户端 | 原 locale／NLS owner；`WB/src/nlsResources.ts`、`WB/scripts/nlsTransform.mjs` 与 `src/clients/shared/core/i18n/` | `test_harness_management_ui/run-325671635b33` 与 `GM/appearance-product-05.json` 验中文及英文重载后的 Provider／外观入口；不外推所有页面翻译。 |
| C06 | 客户端配置档案 | 客户端 | 原 Profiles owner；原 `workbench.profiles.actions.manageProfiles` 注册／Profiles Editor | `test_harness_management_ui/run-325671635b33` 确认真实 Profiles Editor 可达；本轮未验完整导入／导出，不增加账户或云同步。 |
| C07 | 缓存查看、精确清理、恢复说明 | 客户端、精确缓存 owner | 原 IndexedDB `Web/state/threadHistoryCache.ts`；`WB/src/threadCacheActions.ts` 注册 inspect／clear 至 F1 和 EditorTitleContext | Quick索引6的 `ui:protected_recovery`（正式 `quick_access/test_recent_history/run-133b7b960fa4`）验精确 Thread 清理及历史／草稿保留；原 owner 的26项复验见 D8，冻结 Web／Linux development ASAR 不代替当前原生管理验收。 |
| C08 | 关于、版本、构建来源、诊断复制 | 客户端＋明确目标 | 原 About／Gateway diagnostics owner；`WB/scripts/sourceTransform.mjs` 只向原 About 交产品身份；`GatewayDiagnosticsPanel` 复制白名单 | `test_gateway_runtime_observation/run-de69a4bbdc8a` 实测 About 版本／提交及安全摘要；原始日志为独立显式操作，正文和秘密不进入摘要。 |
| T01 | 本地／SSH／远端 Gateway 目标总览 | 本地，可切显式远端 | 原 Gateway registry／`workspaceContext`；`WB/src/sessionNavigation.ts`、`react/navigationView.tsx` 及 `search/workspaceActions.ts` 复用导航 | `test_gateway_connection_management/run-4a8305092941`、`test_workspace_details_management/run-1e9c5ede8884` 验双目标；`test_service_plugin_target_gateway_ui/run-81962f7a3466` 验新启动 Settings 本地A→远端B→A及重新打开的记忆，活动工作区不变；SSH执行／隧道仍为loopback替身。 |
| T02 | 连接新建、编辑、移除、重连及错误 | 入口管理其连接；远端控制面须显式支持 | 原 registry config／manual owner；`boxteam.gateway.connections` → `GatewayConnectionsPanel`／`GatewayConnectionDialog` → 原 routes/workspaces | `test_gateway_connection_management/run-4a8305092941` 验创建／改名／解绑／重连、403／404及取消零写；SSH命令／隧道为 loopback 替身。 |
| T03 | 工作区创建／注册、重命名、移除、打开 | 本地 Gateway，可选远端 | 原 `app/gateway/managed_workspaces.py`／root binding；`boxteam.workspace.details` → `Web/components/workspace/WorkspaceDetailsDialog.tsx` | `test_workspace_details_management/run-1e9c5ede8884` 验目标机器目录、注册／改名／注销、403／409、数据保留；目录观察流阻塞限制见下文。 |
| T04 | Backend 分组、启停、重启、受影响成员 | 本地，可选有权限远端 | 原 `app/gateway/runtime/controller.py`／group_actions；`GatewayBackendRuntimePanel` 消费完整 generation／成员和原生命周期回执 | `test_managed_backend_runtime/run-9814276a9ad9` 验取消、blocked、显式强制、真实409及末成员退出；未逐工作区循环启停。 |
| T05 | 设备及入站访问查看、撤销 | 本地，远端只开放实际支持操作 | 原 `app/gateway/device_connections.py`；`boxteam.gateway.credentials` → `GatewayInboundAccessPanel`，F1／GlobalActivity／AccountsContext | `test_gateway_credentials_management/run-10c4edc622da` 验原设备创建／撤销、外部撤销404重读和关闭停读；Gateway间入站授权只读。 |
| T06 | 连接凭据状态、更新、撤销和归属 | 凭据所属目标 | 原 `app/gateway/credentials.py`／各凭据 owner；与 T05 同命令及详情，不集中接管 SSH／Provider／MCP 凭据 | `test_gateway_credentials_management/run-10c4edc622da` 验有效期／身份、撤销token401及无秘密持久化；其它凭据更新按各原 owner 支持说明。 |
| T07 | 健康、离线原因、日志与设置定位 | 本地，可选远端 | 原 Gateway diagnostics／runtime observation；`GatewayDiagnosticsPanel`、`GatewayRuntimeObservation` → `Web/gatewayApi.ts` 原定位命令 | `test_gateway_runtime_observation/run-de69a4bbdc8a` 验正确目标、request_id和关闭取消；`GM/gateway-failure-context-02.json` 保留错误定位证据，无自动修复。 |
| S01 | Gateway 参数编辑 | 本地 Gateway，可切远端 | 原 Gateway configuration source owner；`WB/src/settings/commands.ts` 的 `boxteam.settings.openTarget/openSource` → 原 Settings／JSONC | `GM/settings-grid-appearance-05.json` 与 `test_mcp_plugin_configuration_ui/run-03f2e5eec5d1` 验 Gateway 原来源预检／CAS／回读；不是客户端设置写入。 |
| S02 | Workspace 用户来源及共享覆盖 | 本地所属环境，可显式选远端来源 | 原 `app/services/infrastructure/config/source_owner.py` 的共享 source／fan-out；`WB/src/settings/sourceOwner.ts`、header／scope 消费 | `test_configuration_shared_sources/run-11763d3b7524` 验双工作区已观察 applied／unchanged、旧基线409及 observed_only；V3组合见D5的继承正式run及原owner引用证据。 |
| S03 | 指定工作区参数编辑 | 本地选工作区，可切远端 | 原 Workspace source writer；`WB/src/settings/editor.ts`／sourceEditor 与 `Web/components/settings/ConfigurationEntities.tsx` 共用模型 | `test_configuration_entities/run-2f639fd63f70`、`test_configuration_shared_sources/run-11763d3b7524` 验固定owner保存／dirty隔离；停机不由Gateway代读。 |
| S04 | 来源、继承、预检差异、恢复继承 | 原设置目标／层 | 原 `config_service/config_source_edit.py` 预检／CAS及 `sourceOwner.ts`；原 `boxteam.settings.openSource/rebaseSource` 消费同一JSONC模型 | `test_configuration_entities/run-2f639fd63f70` 验表单／JSONC同草稿及数组覆盖；`test_configuration_shared_sources/run-11763d3b7524` 验跨上下文CAS；`test_configuration_entities/run-f71e210ba237` 新验Agent下层恢复及Provider整组继承，引用冲突沿D5原owner证据。 |
| S05 | 已保存／已导入／active／pending／失败 | 原设置目标 | 原 config candidate／journal／fan-out；`WB/src/settings/sourceEditor.ts`、sourceOwner 与 `boxteam.settings.sourceStatus` 展示原回执 | `test_configuration_entities/run-2f639fd63f70` 验 source_saved／layer_imported／active或pending_restart；watcher去重15项收据见 D5，不自动重发未知写入。 |
| A01 | Provider 列表、新建、编辑、删除、引用和错误 | 本地 Workspace 用户来源／具体工作区，可切远端 | 原 `config_service/config_source_edit.py`、config_agents；`boxteam.settings.modelsProviders` → `WB/src/settings/entities.tsx`／ConfigurationEntities | `test_configuration_entities/run-2f639fd63f70` 验真实表单CRUD；`out/tests/temp/2026/11/13/provider_source_edits/artifacts/` 核引用／秘密归属；共享／冲突沿D5原owner证据，新增整组继承见 `test_configuration_entities/run-f71e210ba237`。 |
| A02 | 模型凭据／已有登录、状态和显式诊断 | 原 Provider 所属目标 | 原 source writer／秘密占位恢复及既有登录 owner；ConfigurationEntities 密码输入只交原 submission key／edit 回执 | `test_configuration_entities/run-6204e39ea167` 验保存前取消、未知结果仅查原键、唯一PUT／reload／无泄密；保留环境引用，不增加 OAuth Provider。 |
| A03 | Agent 列表、新建、编辑、删除、提示词和引用 | 本地用户来源／工作区，可切远端 | 原 config_agents／config_source_edit 与 Job 快照 owner；`boxteam.settings.agents` → 同来源实体表单及JSONC | `test_configuration_entities/run-2f639fd63f70` 验CRUD；`out/tests/temp/2026/11/13/agent_source_edits/artifacts/` 验快照和当前引用；`test_configuration_entities/run-f71e210ba237` 验同ID覆盖／删除露下层；默认与快照沿D5原owner证据。 |
| A04 | 默认 Agent／Provider 与会话当前选择 | 配置定义／工作区会话默认／精确会话分别选择 | 配置默认→source writer；工作区默认→`config/session_defaults.py`／`app/api/agents.py`；当前选择→Session owner／Composer | `test_configuration_entities/run-2f639fd63f70` 实测取消、两次唯一默认PUT、完整数组／request_id、新旧Session绑定；`GM/session-defaults-evidence-audit-01.json` 明示旧run复用边界。 |
| A05 | 工具目录、两种能力开关、参数说明、策略、显式测试 | 指定 Agent＋工作区，影响同 Agent 会话 | 原 ToolSelectionStore／ToolService／ToolTestService；`WB/src/agent.contribution.ts` 的 tools.focus／revealAgent → `react/workspacePanelView.tsx` | `test_workspace_tool_management/run-52d75ec60736` 验无Session、独立目标、changed回执及首开825.18ms／暖开190.23ms；模型fixture `test_workspace_tool_test_run/run-80d8efee89fe` 为20attempt。原超预算失败及修复边界见 D8。 |
| A06 | Skill 来源列表、配置、刷新、错误与文件 | Gateway／指定工作区 | 原 Gateway skill_sources／Workspace resource_platform owner；`WB/src/resources/skillManagement.ts` 注册两种来源打开和两种刷新命令 | `test_skill_management_commands/run-26687e385129`、`quick_access/resources/test_skill_sources/run-b9bfdb13e6c0` 验原路径和URI；远端Gateway投影／包Skill只读，无副本owner。 |
| A07 | AGENTS／提示词与上下文来源查看 | 工作区；实际上下文绑定 Thread | 原 AGENTS／Skill／context source owner；`WB/src/resources/quickAccess.ts` 的 resource 模式和原文本／Markdown／上下文检查消费者 | `quick_access/resources/test_skill_sources/run-b9bfdb13e6c0` 与 `itemized_context/test_itemized_context_browser/run-a9dc993063f0` 验来源与隔离；不恢复 memory。 |
| P01 | 插件包安装、版本、更新、卸载 | 本地 Gateway，可切远端 | 原 Gateway plugin installation owner；`WB/src/extensions/contribution.ts` → servicePluginManagement／pluginEditor，原安装操作回执 | `out/tests/temp/2026/11/12/plugin_package_scope/artifacts/verification.json` 复用正式安装／更新／卸载；本切片未重写监督或自动更新器。 |
| P02 | 插件按范围启停、新工作区默认 | Gateway／工作区／新建默认 | 原插件配置 binding／runtime owner；servicePluginManagement 原范围／包组动作及固定owner EditorPane | 原 scope 正式证据同P01；`test_harness_management_ui/run-b89a840e80ce`、`test_harness_management_ui/run-c978e5109caa` 验原生列表／范围工具栏；新建默认不改存量。 |
| P03 | 实例状态、错误、重启、日志 | 精确实例 | 原插件实例 supervision／log owner；`WB/src/extensions/pluginEditor.ts`、pluginEditorSections 的实例详情与日志动作 | `out/tests/temp/2026/11/10/plugin_instance_logs/artifacts/verification.json` 复用领域日志；`GM/plugin-mcp-management-acceptance-07.json` 核本次取消信号／迟到拒交边界。 |
| P04 | 插件参数及秘密输入 | 精确插件版本＋配置来源 | 原版本schema／source writer／秘密 owner；`boxteam.settings.pluginParameters`，插件详情与 MCP 参数按钮共用该命令 | `test_mcp_plugin_configuration_ui/run-03f2e5eec5d1` 两范围精确版本／参数、取消零写；Gateway参数不带消费工作区，未把schema压成窗口全局。 |
| P05 | 自有下载源、目录查询、安装进度 | 本地 Gateway，可切远端 | 原 Gateway plugins.sources／inventory／安装owner；`boxteam.plugins.manageSources` → 原 Gateway Settings，原安装菜单／进度 | `test_mcp_plugin_configuration_ui/run-03f2e5eec5d1` 验源预检／CAS回读及空源零catalog／市场请求；无公网源不伪造市场结果。 |
| P06 | MCP 服务列表、添加、详情、连接和停止 | 默认本地管理目标，再指定工作区 | 原 WorkspaceMcpCatalog／connection operation owner；`WB/src/mcp/{commands,service,server}.ts` 接原列表／Server Editor和 `boxteam.mcp.openServer` | `test_mcp_plugin_configuration_ui/run-03f2e5eec5d1` 验添加／详情／齿轮唯一connect/disconnect回执；连接不启停插件；resources/templates明确未提供。 |
| P07 | MCP 授权、取消、私有参数 | 精确服务器＋工作区 | 原 `app/services/infrastructure/mcp/{oauth,private_inputs}.py`；原授权流程／Server Editor／取消与密码输入 | 管理07验参数直达及取消；OAuth／私有秘密提交沿原领域合同，本轮未新验完整授权生命周期，不能以连接按钮通过替代。 |
| R01 | Session／Thread 创建、打开、分叉、重命名、删除 | 精确 owner | 原 Session catalog／session_service；`WB/src/search/sessionActions.ts` 注册rename／fork／delete，nameModes及SessionNavigation打开精确Thread | `out/tests/temp/2026/11/12/session_fork_actions/artifacts/verification.json`、Quick索引6的 `ui:catalog_page` 验真实Session管理和未打开child；不重做聊天。 |
| R02 | 执行状态、队列、子 Thread、委派和停止 | 所选工作区／Thread | 原 Job／AgentExecution／delegation owner；ThreadPane／ChatView、导航与 `WB/src/resources/runtimeActions.ts` 交原停止命令 | `out/tests/temp/2026/11/13/quick_access_action_closure/artifacts/verification.json` 的20flags含精确Session停止、revision冲突／unknown回读；仅已有查询范围，不建汇总调度器。 |
| R03 | 持续任务编辑、启停、等待原因、记录 | 当前工作区 | 原 `app/services/business/persistent_task_service.py`；`WB/src/automation/actions.ts`、原 AutomationInput／pane和底部任务镜像 | 原automation领域12写回执／409／取消证据复用Quick索引6；`e2e:test_goal_sidebar/run-e74a0fec1684` 新验创建／编辑普通任务及两入口同ID暂停／恢复镜像。 |
| R04 | Goal 与会话关联任务、预算和生命周期 | 任务归工作区，关联精确会话 | 原 `app/services/business/session_goal_service.py`／Goal控制版本；`Web/components/cards/GoalManagerCard.tsx`／persistentTasks/TaskGoalDetail | `e2e:test_goal_sidebar/run-e74a0fec1684` 验Goal编辑、取消删除／真删除、reload资源态及会话隔离；未逐一验Goal开始／继续／重新开始全部生命周期。 |
| R05 | 文件／附件预览、多种打开方式、编辑保存 | 精确文件／附件 owner | 原 FileService／attachment owner；`WB/src/fileOpenMenu.ts`、resourceOpener 与原文本／Markdown模型，单击仍内嵌预览 | `quick_access/test_file_canonical/run-722e00646cc5`、`quick_access/test_attachment_media_reopen/run-f9f2229e5da0` 验右键／dirty／CAS及媒体规范URI；不改默认打开规则。 |
| R06 | 文件更改、Diff、审查状态 | 精确会话变更集 | 原 changeset owner；`WB/src/changeFiles.ts`／resourceOpener → 原 Diff Editor共享模型与原审查回执 | `quick_access/test_change_documents/run-c94dc84e3bf5` 验真实更改及URI重开、前后模型共用一次响应、失效拒绝；完整Git SCM受限。 |
| R07 | 终端列表、创建、连接、关闭和会话归属 | 当前工作区，关联资源固定 Thread | 原 Gateway Terminal／PTY owner；`WB/src/agent.contribution.ts` 的boxteam.terminal.focus／workspacePanel与resources/runtimeActions | `test_persistent_resource_lifecycle/run-7bbe1179ddf2` 验真实列表／PTY标记／停止和按需详情正文、精确owner；Quick及Linux ASAR证据沿D5复用。仍是原Terminal容器内React业务，不称原生Terminal模型。 |
| R08 | 浏览器列表、页面、连接、控制 | 当前工作区／关联 Thread | 原 Gateway Browser owner；`boxteam.browser.focus` → workspacePanel／原Browser产品，资源动作固定原Thread／revision | 同一 `test_persistent_resource_lifecycle/run-7bbe1179ddf2` 验当前Thread创建workspace资源、attach／列表／解除／删除及owner；D5保留早先30秒超时、后台orphan竞态与退出日志边界，不称已经修复。 |
| R09 | 端口转发、状态、打开地址和关闭 | 当前工作区 | 原 Gateway port forwarding owner；`boxteam.ports.focus` → workspacePanel及 `app/gateway/runtime/port_forwarding.py` 原路由 | `test_gateway_connection_management/run-216ab6cce99b` 验完整Workbench／双Gateway／Backend／真实localhost SSH创建、打开、右键停止及同owner刷新保留草稿；停止后ECONNREFUSED、原HTTP服务仍可用，非跨机器验收。 |
| R10 | 源码调试配置、断点、变量、栈、控制台 | 精确 Thread＋调试插件 | 原 debugger contribution／DAP owner；`WB/src/debug/contribution.ts` 注册 `boxteam.debug.start` → adapter／原 Run and Debug | 原正式 `test_debug_plugin_workbench/run-693fd5be6fa4` 的Node／DAP成功收据为 `out/tests/temp/2026/11/11/plugin_debug_competing_actions/artifacts/verification.json`；当前adapter／action_dispatch及驱动有后续差异，未冒称当前全链重验；attach／runInTerminal不提供。 |
| R11 | 内容搜索、快速入口、符号定位 | 焦点业务工作区；仅显式扩大 | 原 GatewaySearchService／领域search与上游Search／Outline；`WB/src/search/{nameModes,searchView,searchEditor,threadOutline}.ts` | Quick索引6的 `ui:navigation_page`、`backend:file_search_model` 与 `ui:outline` 分别为真实符号／Search Editor／Thread大纲；管理默认本地不改变搜索显式范围。 |
| R12 | Agent 状态、上下文来源、实际封存内容 | 精确 Thread／执行 | 原 context assembly／sealed turn owner；`Web/components/panels/inspectionPanels/AgentStatePanel.tsx`／contextInspection/AssemblyContextInspector | `itemized_context/test_itemized_context_browser/run-a9dc993063f0` 验选择顺序、loss、reload assembly、活动历史与会话隔离；来源刷新不替换已封存上下文。 |
| R13 | Trace／LLM 请求响应与工具记录 | 精确 Thread／执行 | 原 trace_event_store／turn_history/trace_page、LLM请求日志及工具结果owner；ChatView／resourceOpener和content provider | `quick_access/test_trace_content/run-5446567b9c75`、`quick_access/test_request_content/run-be62f0c9233b`、`quick_access/test_message_content/run-b9c52be618bf` 验有界精确打开／失效；D8另验十万Trace HTTP。 |
| O01 | 产品日志与诊断复制 | 本地所选进程，可切明确远端 | 原 `app/gateway/diagnostics.py` 日志尾读／安全摘要；GatewayDiagnosticsPanel／Output原入口，不把正文并入产品日志 | `test_gateway_runtime_observation/run-de69a4bbdc8a` 与 D8 V7尾读／白名单复验；原始日志为显式单独复制，不作默认摘要。 |
| O02 | 遥测／运行统计配置及现有状态 | 本地 Gateway，可切远端 | 原 Gateway observation／telemetry配置owner；原Settings／JSONC模型与 `Web/components/workspace/gateway/GatewayRuntimeObservation.tsx` | `test_gateway_runtime_observation/run-de69a4bbdc8a` 验同来源预检／CAS开启及真实observation；无页面专用daemon／每请求自检。 |
| O03 | Token、耗时、预算、可确定成本与覆盖范围 | Thread／Goal；聚合须显式选范围 | 原回复／Goal用量投影owner；`Web/components/cards/GoalManagerCard.tsx` 与现有tokenUsage／回复组件 | `GM/usage-presentation-01.json` 的13项合同核最后一次请求／Goal已记录用量、缺价格未知；Goal代表页见R04；跨目标／全历史聚合不提供。 |
| O04 | 存储位置、保留配置、已有维护操作 | 客户端／Gateway／工作区分别选择 | 原Gateway／Workspace存储与保留owner、客户端threadHistoryCache；WorkspaceDetailsDialog／GatewayDiagnosticsPanel及原Settings来源入口 | `test_workspace_details_management/run-1befb76565a1` 工作区节点及 `test_gateway_runtime_observation/run-40cf7da37183` 验位置／保留；storage-product-01整体失败边界见D8；无owner删除／备份／修复不提供。 |
| O05 | 配置／连接故障定位、原因和已有修复动作 | 原失败 owner | 原失败owner／request_id；GatewayControlCenter诊断与日志定位、原通知／Output／配置状态入口 | `GM/gateway-failure-context-02.json`、连接／工作区正式403／404／409原件验owner错误与重读；不把普通运行状态伪装Problems错误，不自动修复。 |

关联职责已按实际消费者核对：Provider／Agent CRUD 只补 `config_service/config_source_edit.py`／`config_agents.py` 和同来源实体表单；Skill 管理只接 Gateway `routes/skill_sources.py`／Workspace `resource_platform` 原 owner。插件安装／范围／监督／秘密仍由 `add-target-aware-extension-management` 承接；Quick Access 的查询／定位／取消和原缓存恢复沿各既有 owner；Goal／目录／消息只消费 `unify-cross-end-state-sync` 已落地回执，不为未落地能力预建桥接。4.7的V3组合按D5收口，7.7的Ports及资源代表闭环见同节；不以关联变更的任务勾选替代本管理链证据。

必要协议的生产闭环已定位：连接目录为 `app/gateway/routes/workspaces.py` → `app/schemas/gateway/management_targets.py`／`proto/boxteam/gateway/v2/public.proto` → Web生成DTO／`gatewayApi.ts` → GatewayConnectionsPanel；存储展示为原 diagnostics／workspace schema与对应 Gateway／Workspace proto → 生成DTO → GatewayDiagnosticsPanel／WorkspaceDetailsDialog。`GM/connection-protocol-check-02.json` 的 format／build／breaking／check:protocol 与 `GM/storage-protocol-generation-01.json`／`storage-protocol-check-01.json` 均 exit0，真实HTTP序列化由对应连接／存储正式run承接。它们是当轮完整快照；本轮复核的Gateway proto仍同SHA，Workspace public／job及已删除的session_interaction／session_stream与旧快照不同，不能把旧全仓生成检查当成当前共享整树通过。没有无消费者的新接口、手写替代DTO或兼容分路。

3.4 的设备／入站凭据切片已接入原 Gateway 控制中心、管理菜单、F1 和 Accounts 菜单；只进入该页时读取原设备与入站摘要。页面展示入口 Gateway、直接管理工作区、授权有效期和连接身份；设备仍用原创建／撤销 owner，Gateway 间入站授权只读，其他凭据按各自 owner 说明支持边界。`tests/integration/clients/web/test_gateway_credentials_management.py` 经真实 Gateway／Backend／完整 Workbench 通过（`gateway-credentials-v1`，1 passed／31.67s）：确认前取消零写入、真实创建与撤销、确认期间外部撤销造成真实404后保留 request_id 并重读、已撤销 token 返回401且工作区保持、关闭后停止读取、本机持久存储无秘密、无模型或审批调用。首次可用853.25ms；原设备列表为本切片唯一代表 API，21样本中的20个暖样本 nearest-rank p50／p95／max为4.60／8.40／15.30ms，响应97B。正式浏览器结果与截图在 `out/tests/integration/clients/web/test_gateway_credentials_management/runs/run-10c4edc622da/nodes/node-c479c795ab91/artifacts/`；准确命令、源码前后指纹和退出码在 `out/tests/temp/2026/11/13/gateway_management/artifacts/gateway-credentials-01.json`。同批真实类型检查和正式 Web 构建通过，消费不可变 `harness_skills_sol/artifacts/final/dist`（main SHA256 `c6c5fb27974cd4b1cc0fc8e43b63ba519b683b1536d8b35833333b00805f3ac6`）。此本地切片不替代3.1连接来源、3.2工作区移除、3.3共享Backend及原生Electron验收。

3.3 的共享 Backend 详情仍由原运行面板和完整组生命周期 owner 提供；新增所属 Gateway、真实 generation、完整成员名称及稳定 ID、保护成员与安全／强制影响说明，不新增详情 API、状态 owner 或逐工作区控制。`tests/integration/clients/web/test_managed_backend_runtime.py` 经真实 Gateway／Backend／完整 Workbench 通过（`shared-backend-management-v1`，1 passed／153.75s）：取消零 POST、成员合并后只展示一个共享进程、真实 SSE 阻塞保留原 generation 且不自动强制、显式强制使旧 SSE 路由失效、确认期间 generation 推进返回真实409并显示原 request_id／Gateway、成员退出及最后成员移除均沿原完整组合同；每个组动作仅一次生命周期请求。首次详情可用132.58ms；原工作区列表（默认健康检查）为唯一代表 API，21样本中的20个暖样本 nearest-rank p50／p95／max为72.90／89.40／92.10ms，响应2783B／2成员。正式结果、原始输出与截图在 `out/tests/integration/clients/web/test_managed_backend_runtime/runs/run-9814276a9ad9/nodes/node-1afdab22bd52/artifacts/`；准确命令、源码前后指纹与exit0在 `out/tests/temp/2026/11/13/gateway_management/artifacts/runtime-product-01.json`。同批真实类型检查、完整 Web 构建及20项原API／生命周期定向测试通过；不可变 `runtime-dist/dist` 的 main SHA256 为 `652f813a6227ed9233c979827689647f881d3c9d6d242c4cf7326cb550c47026`。截图仍保留原生命周期换代期间会话导航错误通知，未据此宣称无关导航全量回归；本地切片不替代远端权限或原生 Electron 验收。

3.1 的连接目录直接投影原 registry，包含 config／manual 来源与真实端口、稳定连接和完整工作区成员；manual 仅用原 POST 修改显示名，config 编辑直达入口 Gateway 的原来源 Settings，解绑仍用原 DELETE，重连沿原 owner，不新增注册表或自动修复。`tests/integration/clients/web/test_gateway_connection_management.py` 经真实双 Gateway／Backend／完整 Workbench 通过（`gateway-connections-v4`，1 passed／76.06s）：创建／编辑／重连／移除前取消零写入，真实改名保留 ID／端口，config 的 manual 删除返回403且来源设置仍属入口 Gateway，确认期间外部解绑造成真实404后保留入口／目标／连接／request_id 并全量重读，重载不重新登记已移除 manual 连接，远端文件与 Session 保留；关闭停止读目录，零模型／审批调用。首次目录可用464.39ms；唯一代表 API 是原管理目录，21样本中20个暖样本 nearest-rank p50／p95／max为4.70／6.50／7.30ms，响应606B／2目标。正式结果、原始输出和截图在 `out/tests/integration/clients/web/test_gateway_connection_management/runs/run-4a8305092941/nodes/node-05cff4de1e04/artifacts/`；准确命令／前后源码指纹／exit0在 `out/tests/temp/2026/11/13/gateway_management/artifacts/connection-product-04.json`。同批真实类型检查、完整 Web 构建、协议 format／build／breaking／生成检查及目录定向测试通过；冻结 `connection-dist/dist` main SHA256为 `5f90f06b83b11ac4dd441484e868304fd58c472e4faf045e668d158176eb5361`。SSH执行和隧道由loopback替身承担，远端 Gateway／Backend／HTTP均真实；不替代真实SSH部署、工作区移除或原生Electron验收。加宽后端检查的3个旧SSH选择测试失败已保留在 `connection-backend-02`，涉及未变更的旧参数与Mock合同，未据局部通过宣称全仓零回归。

3.2 的工作区详情复用原只读镜像、目录选择、重命名、注册／移除、打开和 Settings／诊断 owner，展示冻结入口与所属 Gateway、UUID、Backend generation／完整成员和显式连接；不新增详情 API 或连接注册表写入。`tests/integration/clients/web/test_workspace_details_management.py` 经真实双 Gateway／Backend／完整 Workbench 通过（`workspace-details-v4`，1 passed／115.24s）：同 UUID 的远端详情不激活错误目标，原目录选择返回目标机器的 `boxteam://gateway/…` binding；远端设置403保留目标与 request_id，注册403遵守原不复制远端正文的公开阶段合同，并交叉核目标 Gateway 同 request_id／403。注册、改名与移除取消零对应写入；真实注册展示 Backend／generation，改名后重读，设置固定工作区与来源，日志携完整 owner，打开仍走原激活动作。关闭 Settings、折叠并切走已打开工作区后，原目录观察流仍使安全注销返回真实409 `proxy_stream`；取消原强制确认，显式刷新释放后沿完整 generation／成员合同安全注销200。该既有限制已在 `sessionCatalogOutboxRuntime.ts` 原持有处标 TODO，未宣称修复。注销后刷新不自动重登记，显式再次注册后原 Session 和磁盘文件保留，零模型／审批调用。首次详情可用149.50ms；本切片没有新增或修改列表／详情 API，D8 沿用3.3原工作区列表的20个暖样本证据。正式原始输出、结果和截图在 `out/tests/integration/clients/web/test_workspace_details_management/runs/run-1e9c5ede8884/nodes/node-03de8ae0350f/artifacts/`；准确命令／前后源码指纹／exit0在 `out/tests/temp/2026/11/13/gateway_management/artifacts/workspace-details-product-04.json`，源码与固定产物均未变化。39项相关命令／组件检查、统一真实类型检查和完整 Web 构建通过；消费不可变 `management-dist/dist`，main SHA256为 `cc38d515abd42d423746906d48c0d4d2ac55dd36cbbcd9dd190e9fb6e45ada1e`。SSH执行／隧道为loopback替身，Gateway／Backend／HTTP均真实；不替代真实SSH部署或原生Electron验收，也不把三轮新脚本定位／预期修正称为产品回归。

### D5. 实体 CRUD 是原配置 owner 的完整操作，不是全局 CRUD 平台

Provider／Agent 管理持有稳定对象 ID 和选定来源层，通过现有 source writer 编辑对应 payload；复杂数组／对象可以从详情精确进入原 JSONC，不能把整套配置压成单个字符串弹框作为完整实体管理。表单管理基本身份、必要字段、引用与删除，进阶参数继续原 Settings／JSONC。

Provider／Agent 表单直接编辑原来源 JSONC 文本模型，切换原 Settings／JSONC 仍使用该模型的 dirty、undo、save 与 backup；不另建实体持久草稿、保存队列或二次状态同步。基本身份为既有稳定 ID，创建后不提供 ID 重命名；Agent 已有显示名可编辑，不为 Provider 新加显示名字段。语法错误保留原文并定位原 JSONC，不能用最后一份可解析内容覆盖脏输入。

保存行为沿现有分界：业务来源 URI 继续禁用自动保存，只经显式预检／确认／CAS 保存；不因为原生 Settings 常见的即时写入而给业务来源新增自动提交。客户端原生 Settings 保持上游自身行为，不全局禁用或强制开启 auto-save。原有即时业务开关仍由其对应 owner 提交，不套一层虚假的来源草稿。

编辑先显示本层、继承及生效状态。首版只编辑所选来源实际拥有的定义：Provider 以本层数组为提交基础，数组整体覆盖；本层没有数组时，新增动作先明确确认建立整组覆盖及被遮蔽的继承项，再编辑新数组并按完整 candidate 校验。需要保留继承列表时定位其实际可写来源，不提供复制脱敏 effective 生成覆盖或隐式物化。只读来源只查看。Agent 对象沿递归合并；移除本层定义后可能重新露出下层同 ID 对象，恢复 Provider 继承则移除整个本层数组覆盖。动作据此标明“移除本层定义／移除本层覆盖／恢复整组继承”，提交前展示 effective 变化，遵守至少一个有效 Provider／Agent 等原 schema。没有跨来源彻底删除或 tombstone，不级联修改其他层。

引用检查限定为本次完整 candidate 中的当前定义引用（包括 Agent primary／fallback Provider 及配置默认 Agent）和该工作区现有 SessionDefaults；共享来源只报告 owner 已观察的消费者校验结果，不能承诺未知工作区均无引用。历史 Session／Thread、Job、Trace 和检查点不是 CRUD 的扫描前置，不新增引用索引、全 catalog 枚举或跨 owner 事务。用户从明确 Session 发起时可显示该对象的当前绑定；其余历史引用保留 ID，后续访问已删除定义时按原 owner 报不可用并要求显式选择，不静默换模型、改写历史或中断已持有快照的 Job。必要校验在原 ConfigService／默认值 owner 各自边界补齐，冲突保留草稿；不把多个 owner 的独立操作宣称为原子提交。

Provider 数组 CRUD 的脱敏往返在原 `restore_source_text`／source preflight 中完善，不开 CRUD 专用 writer：仅 `llm.providers[].api_key` 的原占位可定位同一来源原文的旧数组项，核验原占位、唯一稳定 ID 和 endpoint 与 incoming 当前元素一致后还原到新索引。同一规则服务表单与 JSONC；其他秘密路径仍保持原路径和绑定约束，不能忽略失败或清空不匹配值。新项／换 endpoint 要求显式凭据；重复 ID、跨来源占位或占位不符明确拒绝。同一次原预检／来源 CAS 完成验证和唯一写入，不返回真实秘密、不新增操作意图协议、秘密映射表或持久状态。

4.2 的 owner 切片已落地：Provider 密钥占位包含发起 Gateway／Workspace／来源范围，仍是原来源文档中的不透明脱敏值；只从该来源原文按旧占位、唯一 ID 和 endpoint 恢复原 token，保留字符串转义。当前引用检查不展开模型凭据，限完整 candidate 及本工作区 SessionDefaults；同键重试、watcher 认领和来源候选的重启／提升边界复核当前默认值。`tests/integration/backend/config/test_provider_source_edits.py` 与原 source 场景共 46 项 ASGI 集成通过，相关单测 165 项及后续 pending 单测 9 项通过。原始输出、命令及源码指纹位于 `out/tests/temp/2026/11/13/provider_source_edits/artifacts/`；这是 Backend 配置 owner／HTTP 序列化证据，不替代 4.3／4.4／4.7 的真实 Gateway／Workbench 验收。

4.5 的 Agent owner 切片已落地：创建／编辑／移除仍走原来源 GET／preflight／PUT；工作区移除覆盖后恢复下层同 ID 定义，当前配置默认及 SessionDefaults 引用拒绝删除。身份解析不再把 `deep_agent`、空字符串或已删除 ID 换为默认定义，默认模型也不回退第一个 Provider；运行只展开所选 Agent 的 Provider 凭据，并复用同一次配置解码。来源 HTTP 场景21项通过；独立执行场景通过真实 HTTP 创建 Session、发送并在原 model_stream 响应边界删除定义，已开始 Job 继续使用冻结快照，后续发送明确失败，显式切换 Agent 后才继续。相关配置／编排单测147项通过，最终必要补验11项通过（有重叠，不累加）；静态检查相对 HEAD 无新增诊断。命令、源码指纹和原始输出位于 `out/tests/temp/2026/11/13/agent_source_edits/artifacts/`。执行场景为双工作区 AppContainer／ASGI 和真实模型 transport 回放，不能替代4.6／4.7的 Gateway／Workbench 管理验收。受影响后台时间任务单测另有1项注册时序失败，尚未修复，未宣称整组回归通过。

4.3 的 Workbench 表单切片已在 `ec176bd8a` 本地提交：Provider／Agent 的列表、详情、新建、编辑、移除直接作用于原来源 JSONC 模型，保留 undo／dirty／显式保存及完整来源回执；Provider 整组覆盖和继承恢复均明确说明，Agent 同 ID 下层覆盖须确认，inline 提示词解除继承文件绑定。Composer 与管理表单可在同一 Gateway／Workspace 的明确 Thread 间双向到达。真实 Gateway／Backend／完整 Workbench 场景 `tests/integration/clients/web/test_configuration_entities.py` 的 CRUD、重载及精确会话选择器返回通过（`gateway-v4`，1 passed）；同批真实类型检查 `typecheck-v7`、Web 构建 `build-v2` 通过，原始命令、源码指纹及结果位于 `out/tests/temp/2026/11/13/entity_forms/artifacts/`。本轮复核发现该 UI run 删除的是新建 ID，未实际证明 Provider／Agent 下层继承恢复；4.7补验复用真实 Workbench UI、Gateway 与配置 owner，不把既有 HTTP 测试冒充 UI。

定义的 `default_agent` 等沿 source writer；工作区会话默认 Agent／Provider 沿原 SessionDefaults API 和存储；当前 Session 选择沿原 Session owner，与 Composer 共用相应既有操作。不迁入同一个 JSONC、双写或新增默认值协调器。原工具状态操作沿已有 writer。Provider 最小配置不新增采样、reasoning、输出上限；工具只展示真实 execution_enabled／model_visible 联动，不附加审批体系。只在用户点击诊断／测试时执行实际网络或工具调用，并标明可能有外部等待或费用，不后台预检全部模型。

4.6 已由 `1f4a9bf16` 的原 SessionDefaults 接线交付，本轮复核正式 `test_configuration_entities/runs/run-2f639fd63f70`：真实 Workbench／Gateway／Backend 的表单、JSONC 与 Composer 双向入口可用；来源 `default_agent` 保持未保存草稿时，默认值确认取消零 PUT，随后 Agent／Provider 默认各沿原 API 唯一 PUT，完整 Agent 数组回执及 request_id 正确。来源正文与 digest 不变，原 Session 绑定不变，新 Session 采用新默认；零模型请求、`pageErrors=[]`、浏览器进程退出0。该场景的 `/api/v1/agents` 20个暖样本 p50／p95／max 为26.40／36.10／47.90ms，规模4个 Agent、响应2722字节。默认值、定义和当前选择继续各沿原 owner；后续 `e90228c80` 删除独立配置默认编辑器，`c1212b200` 补工具直达，没有重建默认值 writer。准确结果 SHA、当前依赖审查和复用限制见 `out/tests/temp/2026/11/13/gateway_management/artifacts/session-defaults-evidence-audit-01.json`；这是历史正式 run 的复核，本轮未重跑，也没有以旧结果声称当前整树／并行 UUID 改动已经验收。

来源保存后等待显式重启的 watcher 重复认领已在 `8547fd09f` 修正：全部实际来源仍等于原候选基线时，不再重新执行该保存；其它工作区来源变化仍进入原 reload，不丢掉变化，也不改写原提交的 candidate／journal／fan-out。重复物化保留已报告的 `restart_required`，不反复刷新回执时间。原 `test_workspace_source_owner.py` 15项通过（36.06s）；准确保护命令、原始输出、源码及候选指纹在 `out/tests/temp/2026/11/14/config_source_watcher/artifacts/`。这是原 owner 的去重修复，没有增加 writer、账本或恢复框架。

工具定义策略沿原配置来源；两个运行时开关沿工作区 `ToolSelectionStore` 按 `agent_id` 保存，作用于该工作区使用此 Agent 的会话，不是当前 Session 私有配置。工具参数 schema 用于说明及原测试，不把只读工具定义变成可任意编辑的 schema。策略锁定与插件／MCP 可用状态分别展示，开工具不启动插件，停插件不改 Agent 定义。selection PATCH 只回 changed 项：以 `tool_id` 完整替换对应行，其余目录保持；失败沿原 owner 重读，不为每次成功强制再取全目录或把部分回执冒充完整目录。工具测试先明确选择非空 Provider 范围、已有重复次数和潜在模型调用费用，再按原请求提交；保留后端空数组表示全部的既有合同，新管理 UI 不用空列表冒充单模型测试。进度按发起时工作区及 run_id 查询，关闭页只停止观察，不假报测试取消或自动重启测试。

Skill 来源配置分别沿 Gateway／Workspace source writer；刷新是原来源 owner 的显式获取，不等于启停插件、修改 Thread 跟踪或替换封存上下文。Gateway 远端来源投影目前只读，不借管理 UI 新增联邦刷新 RPC；本机 Gateway 与 Workspace refresh 分别走已有合同。包内 Skill 以原登记 URI 只读打开；已有可写 Skill／AGENTS 按实际 owner 编辑，无可用写能力时说明原因。本变更不承诺包 Skill“创建可编辑副本”按钮、不复制整包资源、不新增副本 owner 或来源启用账本。

A06/A07 代表入口验收已通过，`tasks.md` 的5.2–5.4只据此闭合。A06 Skill 命令真实 Workbench／Gateway 场景 `test_skill_management_commands.py` 最终1 passed／26.01s，结果 `out/tests/integration/clients/web/test_skill_management_commands/runs/run-26687e385129/nodes/node-b734d805d15a/artifacts/skill-management-result.json`（SHA256 `5b634f592ca7ab37a072bbe1cca67ee0319c4d888a716670b330768c293f8498`），记录本机 Gateway 与 Workspace 各自走原配置来源 owner、刷新分别为 `/api/gateway/skill-sources/refresh` 与 `/api/v1/resources/skill-sources/refresh`，Workspace 请求带同一 Gateway／Workspace UUID，且通过正式 URI 打开。A07 真实 `test_skill_sources.py` 1 passed／112.82s（进程保护命令、exit0 在 `out/tests/temp/2026/11/13/harness_skills_sol/artifacts/pytest-a07-skill-sources.log`），浏览器回执 `out/tests/integration/clients/web/quick_access/resources/test_skill_sources/runs/run-b9bfdb13e6c0/nodes/node-d5e4694fba49/artifacts/skill-sources.json`（SHA256 `43972432af3965d06d2527720fa7d36ebafd0bd157a0781ba1ee6065f3aca0ad`）：local/HTTP/package Skill 通过原 discovery／只读 URI，取消、显式 refresh 和错误沿原 owner，无无授权副作用。上下文检查以真实完整 Workbench／Gateway 场景 `integration-web-context-inspection` 1 passed／129.42s、matrix elapsed 134.50s（run `out/tests/runner/run-tests/artifacts/2026/11/13/run-a9dc993063f0/integration-web-context-inspection/run.json`，源码前后 SHA见该收据）通过；browser receipt `out/tests/integration/clients/web/itemized_context/test_itemized_context_browser/runs/run-a9dc993063f0/nodes/node-c9832098be92/artifacts/browser-result.json`（SHA256 `a82f4eef777b2d16e99dc6214b4335b586cf06e60d23c1c74fc25a40690c5b32`）确认 selection order、loss、reload assembly、active history、session isolation 与 network routes 全部为 true，page errors为空；使用的完整 Workbench main SHA256 `3882e48c5cd57e11939f61005e95eb3f4ba539d4c80e3969aa591f8e6d6ea6ee`。

5.5 的只读复核见同一 Skill 浏览器回执：来源列表沿原读取路由，打开现有 Skill URI 使用 `operation=read`；打开过程没有触发来源刷新、Session 创建或模型请求。Gateway 原生／Relay Skill metadata 仅作只读投影，不刷新或发布远端状态；显式刷新仍由本机 Gateway 与 Workspace 各自原 owner 处理，不扩展包 Skill 的写入能力。

2.4 的原导航 owner 是 Workbench `SessionNavigation` 与 `NavigationSurface`；现有 Workspace／Session 命令在 `search/workspaceActions.ts`、`search/sessionActions.ts` 显式捕获稳定 owner 后转交原导航／编辑器 owner。Gateway 入口在既有 `boxteam.gateway.connections`／`boxteam.gateway.credentials` 命令及原 SessionNavigation 节点，设置入口在既有 Provider／Agent Settings 命令。欢迎页缺少 Harness 直达，新增 `harnessWelcomeTransform.mjs` 仅转换固定上游 `gettingStartedContent.ts` 的原 `startEntries`，分别直达 Provider、Agent、Gateway Connections 原命令；无 Session 即可用，不创建或激活 Session／Workspace，也不加通用转发命令。除独立转换回归 `harnessWelcomeTransform.test.mjs` 1 passed／exit 0 外，`run-325671635b33` 真实 Workbench 页面实际打开并点击三个 Welcome 入口，验证无 Session／活动工作区改变，原命令 owner 正确且无插件全目标清点／审批轮询；同一运行验证无 Session 情形和 Settings／MCP Backend 未就绪显错、没有配置读取的相关行为（`tools-management-scope-01.log` 27 passed／229 expect）。据此 2.4 闭合。

2.5 在真实 Workbench `run-325671635b33` 检查管理菜单与 Settings 外观分类、快捷键页、原 Profiles 页、Python 文件类型检测及中英文 Provider／Appearance 页面；`appearance-product-05` 复验外观入口与英文页，`settings-grid-appearance-05` 真实 Settings／JSONC 表单和分屏时保留 dirty 来源。独立验收均 exit 0；后两项复用的冻结产物为 build09，web 协议源 mismatch 列在收据中，不据此宣称本轮产品源码完整构建。2.5 只交付实际已有客户端能力，不恢复用户档案或云同步。

2.1–2.3 的实际证据按覆盖范围记录。管理目标入口合同由 `tools-management-scope-01.log`（27 项通过、229 assertions）覆盖：设置、MCP、工具入口本地默认及入口/Gateway 目标记忆、工作区记忆和失效行为。真实 `run-3b1f25ae9915` 的双 Gateway 插件 UI 验证 B 延迟写入只写 B、A 保持原值、释放后原 Gateway 页面 owner 不变；`run-6cce9a6466e9` 真实工具入口验证默认目标工作区与导航 activeId 分离、直接切换且不激活工作区。新启动 Settings 路径另由 `test_service_plugin_target_gateway_ui/runs/run-81962f7a3466/nodes/node-3a2ba21aa11e/artifacts/settings-target-gateway-result.json` 覆盖：正式双 Gateway／Backend fixture 与同 UUID Workspace 均真实；首次 Agent Settings 从本地 Gateway A 的用户选择导航，按每次 `/api/v1/config/sources*` 实际请求头核 owner，显式下拉切至远端 Gateway B 后只读 B，切回 A 并重新打开全局 Agent 入口仍读取 A。活动工作区响应前后相同，pageErrors／外部请求均为空。browser exit 0；同节点 `settings-target-browser-process.json` 记录子命令 exit 0、两个实际 Gateway ID 与 connection ID，`settings-target-workbench-assets.json` 记录复用 build10 的 main SHA256 `4807dc8818c4bae4b71f98528e563c5b1c1bf7ed7491475387291745383e1390`。外层进程以 timeout 300 秒及 4 GiB 数据段限制保护；内部复用 E2E runner 的 4 GiB／180 秒浏览器监督。远端 SSH 执行／转发仍按该 fixture 的 loopback 替身边界，不据此声称真实跨机器 SSH。

2.2／2.3 由 `tests/integration/clients/web/test_configuration_shared_sources/runs/run-11763d3b7524/nodes/node-443e1da5c69d/artifacts/result.json` 的真实 Workbench／Gateway／双工作区结果闭合：Gateway、workspace 私有来源及共享用户来源分别携准确 owner；fan-out 明示 `observed_only`，两个已观察消费者分别返回 applied／unchanged；B 保存旧基线在预检返回 409，PUT 数不变且草稿保留；管理入口选择首个工作区后切至 B，A 的分屏草稿保持 dirty，切换无写入，导航 active workspace 未被设置打开改变。页面错误与模型请求均为空，browser exit 0。正式进程记录 `browser-process.json` 显示外层 180 秒超时及 `ulimit -d 4194304` 内存限制。

4.1 的 Agent 未接入配置项已在权威 `configs/workspace_schema.jsonc` 的 `agents` 描述明确列出：`timezone`、`persona`、`safety`、`env`、`tools.tool_choice`、`tools.timeout_ms`、`execution.stream`、`execution.max_turns`、`execution.max_history_messages`、`execution.max_context_tokens` 保存后不改变运行行为。检查确认 Agent 是动态 `additionalProperties` map，原 Settings schema 投影不会将其展开成独立设置字段；实体表单只编辑已消费字段，因此没有为其增加路径过滤或修改输入 schema。`settings_editor_target.test.mjs` 中 Backend 未就绪、来源 schema／JSONC 模型、dirty、保存及分屏路径的 27 项测试通过（229 assertions，`tools-management-scope-01.log`）；真实 `run-11763d3b7524` 验证共享来源预检／CAS 和未应用状态说明。Agent／Provider 表单的完整实际保存与运行状态回执复用 `test_configuration_entities/runs/run-2f639fd63f70/nodes/node-35534b509a30/artifacts/result.json`：真实来源 writer 分别返回 source_saved／layer_imported 和 active 或 pending_restart 状态；Agent 变更明确报告 deferred path 与 restart_required。产品首次可用 681ms，无页面错误或模型请求；Provider／Agent 字段编辑、密码仅沿原 submission key 查询且仅一次 PUT、SessionDefaults 与精确 Session 选择分别走原 owner。原件的 browser-process 记录命令 exit 0。schema 描述的变更不改变存储或运行合同，也未重新构建客户端产物；Linux Electron 原生管理保存／重启已补验，准确产物及未验平台见D8。

4.7/V3 按 owner 边界收口复核：真实 Workbench `test_configuration_entities/run-2f639fd63f70` 原已证明 Provider／Agent CRUD、密码提交恢复及 SessionDefaults；新增完整真实 Workbench 场景 `test_configuration_entities/run-f71e210ba237`（1 passed／105.83s，完整 build10 SHA256 `4807dc8818c4bae4b71f98528e563c5b1c1bf7ed7491475387291745383e1390`）保留原密码、CRUD、SessionDefaults 与 Composer 选择断言，并先经原来源 owner 写入受控下层 Agent／Provider，再由实体表单确认同 ID Agent 覆盖、保存并移除本层定义，核验下层名称与提示词重新显示；Provider 表单移除实体后恢复整组继承，核验本层 `llm.providers` 消失且 `source_effective` 与重新读取的下层来源整组相同。所有来源写入沿既有 preflight／CAS，测试 seed 单独计数，不混入无自动保存断言；`result.json` 标记 completed、零 pageErrors、零模型请求。正式原件位于 `out/tests/integration/clients/web/test_configuration_entities/runs/run-f71e210ba237/nodes/node-35534b509a30/artifacts/`；原始输出和退出码在 `out/tests/temp/2026/11/14/settings_remote_gateway/artifacts/v3-inherited-settings-ui-16.log`／`.exit`；准确保护命令、结果SHA及事后源码指纹审核见 `out/tests/temp/2026/11/13/harness_matrix_audit/artifacts/settings-inheritance-evidence-01.json`。该run开始前没有全量源码指纹清单，不以事后审核冒称运行期全树未变；本轮脚本解析和diff检查通过。 真实配置 owner／ASGI `out/tests/temp/2026/11/13/provider_source_edits/artifacts/integration-v5.log` 记录46 passed（`test_provider_source_edits.py` 与 source 场景）；覆盖 Provider 重排后的凭据归属、创建／移除与旧 CAS、primary／fallback／配置默认／工作区默认引用拒绝、外部冲突和来源绑定。`out/tests/temp/2026/11/13/agent_source_edits/artifacts/integration-v2.log` 的 `test_removed_agent_keeps_running_snapshot_and_requires_explicit_session_choice` 为1 passed：真实 HTTP 创建会话、在 `model_stream` 响应边界移除定义，进行中 Job 保持冻结快照，后续发送明确失败，用户显式选择后恢复；同目录 `integration-v1.log` 为失败旧运行，不作为通过证据。双工作区 Workbench `test_configuration_shared_sources/run-11763d3b7524` 证明 A/B 的共享来源只报告 `observed_only`，两真实消费者分别 applied／unchanged，B 旧基线 409、无 PUT 且保留草稿；没有推断未知消费者完成，也没有把该 run 说成引用冲突 UI 验收。未复跑上述已有效领域检查。新启动 UI 专项 2.1 的准确收据是正式 `test_service_plugin_target_gateway_ui/run-81962f7a3466/nodes/node-3a2ba21aa11e/artifacts/settings-target-browser-process.json` 与其兄弟 `settings-target-gateway-result.json`（Browser 子命令 exit 0，build10 SHA256 `4807dc8818c4bae4b71f98528e563c5b1c1bf7ed7491475387291745383e1390`）；外层 pytest 原件是 `out/tests/temp/2026/11/14/settings_remote_gateway/artifacts/settings-only-04.log`／`.exit`（1 passed／91.89s，exit 0）。`acceptance-04.log` 是早先插件场景失败记录，不能作为该 Settings 专项收据。Agent 当前默认／SessionDefaults 引用冲突与多工作区共享交互分别保留各自 owner 证据边界，不宣称跨 owner 原子提交。

第 7 组 R01–R13 源码入口核查已完成：Session／Thread 由 `sessionNavigation.ts`、`search/sessionActions.ts` 与 `search/quickAccess.ts` 持有；持续任务由 `automation/actions.ts`、`PersistentTasks` 与 `TaskGoalDetail` 使用同一 Gateway／Workspace 身份；Goal 编辑及控制复用 `GoalManagerCard` 与同一 Goal owner。文件／附件沿 `resourceActions.ts`、`fileOpenMenu.ts`，Diff 沿 `changeFiles.ts` 的原 Diff Editor；Terminal、Browser、Ports 视图与入口由 `agent.contribution.ts`／`workspacePanel.ts` 接回原工作区资源路由；已登记 DAP 由 `debug/contribution.ts`／`debug/adapter.ts`；搜索、Search Editor、Outline 由 `search/*QuickAccess.ts`、`searchView.ts`、`threadOutline.ts`；上下文来源由 `AgentStatePanel`／`AssemblyContextInspector`，Trace／LLM 请求由 `ChatView`、`quickAccessFederation.ts` 与 `resourceOpener.ts` 定位至原 Thread／资源 owner。没有发现需要新建 registry、业务 API 或第二份导航状态的入口缺口，7.1–7.6 的入口核清与本节资源／Ports代表场景、D4原领域证据共同闭合7.7；不将纯注册核对当作独立业务验收。Goal 卡片可见开始、编辑、暂停、继续、重启、删除和用量操作，但本轮没有逐一通过真实后端覆盖全部生命周期转移的集成证据，不能据此宣称 Goal 全生命周期验收已完成。

7.7／V5 的 Terminal／Browser 当前管理入口已通过 `test_persistent_resource_lifecycle.py[management-open]` 的正式 `run-7bbe1179ddf2`：1 passed／472.57s（含共享集成闸门排队），runner 475.71s、exit0，页面场景20.252s。经真实 Gateway／Workspace／Thread 创建 Terminal，核列表省略 `display_buffer`、真实 PTY 标记、停止后的按需详情正文及可见最终输出；Browser 经当前 Thread 创建工作区资源，核真实 attach、精确 owner 列表、解除关联与删除。页面／console／HTTP错误为空，前后受影响源码不变，当前完整插件包源码无差异。复用 build10，Terminal／Browser 资源消费者与资产指纹一致；唯一已变化的插件详情错误边界不在本场景路径，本轮不外推该插件改动。正式结果 `out/tests/integration/clients/web/test_persistent_resource_lifecycle/runs/run-7bbe1179ddf2/nodes/node-f814fbd7e416/artifacts/management-open.json` 的 SHA256 为 `142753ef1ce4164a1dbef841c87ff4d4741ebee5c3555ba84879100d94a1cc01`；完整保护命令、环境和前后指纹见 `out/tests/temp/2026/11/13/harness_matrix_audit/artifacts/resource-management-product-07.json`（SHA256 `82dc0c31251caf620d7d341c9a806c6e798e9f40b74dc16480859f1b86c5e83d`）。两测试文件已本地提交 `de31eb844`，ruff／Bun解析／diff检查通过，最小复审及静态证据见同目录 `resource-final-review-01.json`／`resource-static-03.json`；新增参数由fixture回收隔离环境，不借Session删除作为额外业务清理，原两参数的删除断言保留。

该run只证明上述资源代表行为：启动前仍记录 managed Backend 尚未运行的插件配置交接错误及绑定未就绪409；后台 orphan sweep 曾在同一 Browser 创建尚未登记 ledger 时产生 KeyError，随后原创建返回200；页面完成后仍记录 file-watch EOF／exitcode=-15 与关闭失败。上述启动、后台恢复与关闭域未记全绿，本轮未扩展修复。原卸载／重启完整场景未重跑；早先v5 Browser创建30秒超时原因仍未确认，后续成功不构成修复证明。

7.7／V5 的 Ports 已补完整真实入口：`test_gateway_connection_management.py::test_ports_create_open_and_stop_real_ssh_forward` 正式 `run-216ab6cce99b` 为1 passed／382.75s、外层386.15s（含共享集成闸门排队），pytest与browser均exit0。完整Workbench经双Gateway／Backend与真实localhost SSH创建转发；填写三个字段后显式刷新工作区状态仍保留草稿，原地址链接打开的真实HTTP正文正确；右键停止后列表为空且本地地址ECONNREFUSED，原HTTP服务仍200，精确Gateway／Workspace与request_id均吻合，`pageErrors=[]`。正式结果 `out/tests/integration/clients/web/test_gateway_connection_management/runs/run-216ab6cce99b/nodes/node-069e03e00537/artifacts/result.json` SHA256为 `685810cac9d5fd9d8fd57de71061e5b66682f97b78ede93b370f53d804ad752e`。发现的产品缺陷是同owner列表快照或面板重新激活会重置草稿；修复仅拆开原owner重置与列表刷新两个effect，真实owner变化继续清除草稿，保留原revision隔离，无新状态／重试／持久化。旧实现新增回归明确失败；修复后13项／77断言、Ruff、脚本解析、真实Web类型检查及独立Web构建通过，本地提交 `cb17ac657`。准确保护命令、18份相关源前后指纹、原始输出和最小复审在 `out/tests/temp/2026/11/13/harness_matrix_audit/artifacts/ports-product-07.json`、`ports-final-review-01.json`；新构建main SHA256为 `c0b5c5778e74a5fa5c2295b86c0044b934b09fc5d965c68af3c7333464479653`。1102份前端快照仅他人的SSE测试文件在产品运行后变化，运行／构建源码和本次四文件未变。Workbench全投影类型检查180秒外超时exit124且没有类型诊断，不记通过；本次仅改Web组件，其真实类型检查已过。不据此声称跨机器网络或其它Ports生命周期通过。7.7以本节资源／Ports与D4逐项原领域代表证据闭合，不重写领域生命周期。

5.1 的真实工具管理入口通过 `test_workspace_tool_management.py` 验收：导航活动工作区保持 B，默认管理目标为本地首个 A；原下拉切 B／A 后仍不激活导航，Agent 定义直达固定 A／default。三个工具 PATCH 每次仅回一个 changed 实体，其余目录保持，不追加全目录读取；取消模型测试零 POST。正式 `run-6cce9a6466e9` 为1 passed／40.79s，浏览器 `pageErrors=[]`，结果 SHA256 `3cb296f2abdc61d247f616f89e5fc7d7960c663db13b18d913c6d6c1c9c10056`。工具管理目标使用原入口 scope／Gateway 工作区记忆，不再跟随导航 activeId；对象入口不改变全局选择。功能提交 `304109fb5`，准确命令与指纹在 `out/tests/temp/2026/11/13/gateway_management/artifacts/tools-management-target-product-01.json`。

模型测试通过原 Workbench／Gateway 产品入口选择非空 Provider、两次重复和费用确认，只提交一次；重载后观察原 run，完整十个正式 case×两次重复共20个唯一 attempt，每项一次模型调用、零重试。固定 model_stream 只为首个 case 提供正确参数，真实结果为2成功／18失败／10%，界面与磁盘一致，未伪造全绿。正式 `run-80d8efee89fe` 为1 passed／35.19s、无页面错误，结果 SHA256 `94ceee910d71e15d8482b1b641e881c4b3a28593b6fc6f33a479a416b78fe27b`。已复现的同步模型冷构造3.31s会阻塞2s健康窗口，提交 `b5060108d` 仅沿 `asyncio.to_thread` 执行原 SDK 构造，保留原健康期限、provider串行与run身份；8项原运行单测通过。两场景共用已通过真实类型检查13及 Web build10（main SHA256 `4807dc8818c4bae4b71f98528e563c5b1c1bf7ed7491475387291745383e1390`）。准确命令、原失败、冷构造探针及源码范围在同目录 `tool-model-run-product-04.json`／`tool-model-delivery-01.json`；验证期间仅并行配置来源编辑文件变化，工具运行相关45份源码与前端输入未变。本项不代替远端凭据完整矩阵、原生 Electron 或其它领域生命周期验收。

秘密写入分别核对 Provider、连接、插件、MCP 的现有合同，不能互换引用格式。Provider 首版的完整路径固定为：选择原配置来源与稳定 Provider → 原 source document 读取脱敏内容和 CAS 基线 → 非持久化密码 Quick Input → 仅在内存构造更新 `llm.providers[].api_key` 的原 source writer 请求 → 原预检、确认及 CAS 保存 → 消费原脱敏回执，丢弃输入。字面量仍写既有目标侧 JSONC／原配置状态，显式环境引用仍由目标配置 owner 解释；不创建 Provider 私有文件、另一引用格式、秘密数据库或新认证协议，也不宣称已有存储加密。

已有脏来源先沿原保存／取消流程结清，密码不可进入能备份的文本模型、撤销历史或浏览器持久草稿。预检差异、确认和错误均使用脱敏内容。密码流程只在发出保存前的取消保证零写入；保存已发出后关闭、断连或 Abort 不能撤销提交。丢弃明文，只保留原 submission key／非秘密基线，沿原 source edit 回执查询 `source_saved`、candidate 和文件版本；明确区分原提交已保存与后来来源又变化。不能用明文请求与脱敏 `document.jsonc` 的字符串相等判断，也不持久化明文／客户端秘密摘要来解决比较。结果未知时不重发 PUT、不换键、不报取消成功，恢复页面仍只查询原键；明确原结果后才能重新确认下一次输入。冲突不自动重发秘密。普通编辑的占位恢复遵守上文同一来源及绑定规则。插件／MCP 的受保护引用和现有 ChatGPT 登录保持各自 owner，不新增登录方式；连接等无既有安全写入合同的能力只给支持说明，不趁此统一秘密系统。

4.4 的非持久密码入口已沿同一原来源 writer 落地：先结清脏草稿，密码 Quick Input 只构造本次内存请求；确认前取消零 PUT。原 Workbench 本机存储只保留该来源的原 submission key 与发起连接路由，热退出备份仅保留 CAS 基线；未知请求重开后仍沿原路由查询，不重发 PUT、不另存秘密摘要。原 edit 的 `source_saved` 与 `edit.file_digest`／当前 `file_digest` 区分原提交事实及后来修改，避免明文与脱敏 JSONC 比较；普通保存的脱敏占位归一只在原文本生命周期中刷新干净模型，后续草稿不被覆盖。真实 Gateway／Backend／Workbench 的 `secret-gateway-v1` 通过（1 passed，64.13s），经产品入口产生密码提交并故意丢失真实保存响应与首次查询，重载后仅查询原键且 PUT 总数为1；同时覆盖两种取消、真实外部 CAS 冲突恢复、文本／撤销与本机持久存储无密码、owner／request_id 和零模型请求。32项来源传输及原 Settings 定向检查、真实 Workbench 类型检查和 `secret-final-dist/dist` 正式构建通过；最终完整回归 `run-6204e39ea167` 为1 passed／64.03s，起终不可变dist指纹一致。此前第二次reload只等Settings恢复便发命令，早于导航Gateway身份初始化；测试改等原目标工作区导航根节点，原失败与修正后原件均保留。最终收据位于 `out/tests/temp/2026/11/13/contribution_closure_luna_max/artifacts/entities-readiness-20261113-001/`，本地功能提交 `78c51d9dd`；准确命令、源码指纹和原始输出沿 `out/tests/temp/2026/11/13/entity_forms/artifacts/` 及 `out/tests/temp/2026/11/13/state_sync_catalog/artifacts/` 保留。普通密码合同没有新增登录方式；实际跨远端目标的完整管理矩阵仍按V4验收，不由这条本地场景替代。

### D6. 有界展示与最少生命周期机制

列表只取原 owner 的当前小摘要／页；正文、Trace、日志、工具结果和配置详细诊断按用户展开加载。优先复用已有观察事件和可见性生命周期；管理页不新增常驻全局轮询、审批查询、全目标探活或订阅管理服务。原 MCP 等已有必要可见刷新沿其 owner，不把本变更变成无关监督重构。

隐藏／关闭仅释放本页读取和监听引用；共享订阅按原 owner 最后观察者释放，脏工作副本继续原生命周期。失效的旧查询结果丢弃，不将页面取消当作已发出保存、任务或实例执行的取消；副作用仍沿原回执收敛。

| 校验事实 | 权威 owner | 必要触发边界 | 失效／结束条件 |
|---|---|---|---|
| 当前目标／授权／路由 | 原 Gateway registry 与凭据 owner | 读取／命令准入，消费已有失效通知 | 目标／连接／授权换代；原操作明确终止，不换目标重放 |
| 配置来源及引用 | 原配置 owner | 正式预检／提交及来源变化；不全目录读审计 | 来源 revision／digest／schema 变化，沿原 CAS 冲突 |
| 插件 schema／目录 | 原包／配置／发布 owner | 已有版本／发布变化；仅当前管理范围消费 | 安装版本或发布变化；不每页全量重新 hash 包 |
| 列表和历史页 | 各原资源 owner | 可见范围读取／显式续页／原事件失效 | 原页版本／cursor 失效；不扫描完整存储恢复 |

不为这些事实增加本变更专用账本、第二缓存、跨 owner 事务、恢复器或自动回滚。部分目标离线显示该范围事实；操作结果未知沿原回执查询，不因重开页面另发副作用命令。

移除工作区注册、断开连接及删除会话是不同动作。工作区移除沿原控制面合同，不删除工作区根或 `.boxteam`，不伪装为关闭当前 Tab；涉及共享 Backend 时按原 generation／完整成员及阻塞回执展示影响，不能逐工作区循环 stop／restart，blocked 不报成功或自动强制。连接移除遵守其 config／manual 原 owner，不顺带停远端机器。既有详情／脏草稿在 owner 不可达后保留原归属与错误，不换目标、不自动重新注册。

### D7. 目录与注册点复用

`workbench/src` 负责命令、ContextKey、菜单、EditorInput／EditorPane 和目标上下文；业务表单与可复用 DOM 继续在 `web/src/components` 对应领域，hook 继续原领域；共享纯模型只在存在实际复用时放 shared/core。优先扩展现有 `settings/`、`extensions/`、`react/` 及 Gateway 组件，只有复杂实体确需独立输入时才在相应领域建立小目录，并补四段式 AGENTS.md；不预建通用页面工厂／全局管理 router／配置 bridge。

VS Code 复用等级必须如实登记：Settings／JSONC／Diff／MCP／DAP 有原生服务接缝；目标管理、Provider／Agent 详情使用原生外壳加业务内容；Terminal／Output 目前为原容器加 React；浏览器没有可直接照搬的完整原生页。Profiles／Accounts／Ports 的 UI 位置不代表具备同步、身份或 Remote Extension Host 能力。

### D8. 验收与预算

沿 `enforce-interactive-latency-budgets`：本机回环、服务已就绪、无外部等待，≤512KiB 暖 API p95≤500ms／max≤1500ms；三步导航 p95≤1000ms／max≤2000ms；普通管理详情首次真实可用≤3000ms；原 Composer／文件正文打开≤2000ms及更严格 reader 预算不变。计时到正确目标的当前数据与控件可用，不以缓存占位／骨架结束。生命周期启动、用户输入、外部模型／网络等待单独报告；本产品内部 Relay、队列和校验不能排除。

复用现有真实 Workbench、Gateway、工作区与回放 fixture；普通模型管理验收不需要发付费模型请求。最小有判别力的场景组：

| 场景 | 关键观察与错误实现对照 |
|---|---|
| V1 无 Session 本地入口＋远端 Thread 获焦 | 全局 Provider／Settings 仍本地，具体 Workspace首次默认首项并显示，切回恢复本入口上次选择，无远端业务请求；明确选择 B 后才请求 B；Backend 未就绪显示错误及原显式入口，不显示假空列表或静默启动；ready owner 的定义管理不被单个缺凭据 Provider 阻断 |
| V2 双目标草稿与路由失效 | B 草稿与 A 同名对象并存，B 保存只写 B；上下文命令显式owner且await期间焦点切换不改归属；同来源表单／JSONC共用模型、业务来源不自动保存，客户端原生设置保持原行为；共享源两工作区各自CAS，断开 B 不重投 A或重放副作用 |
| V3 原来源保存／继承／共享 | 创建 Provider、Agent 引用、修改及重读；数组覆盖／恢复整组继承、Agent 移除覆盖后露出下层；重排保持密钥归属、换 endpoint／跨来源占位拒借；共享来源已观察结果，未知不伪报完成；配置默认／SessionDefaults／当前选择分别写原 owner；primary／fallback／默认值冲突不触发历史扫描 |
| V4 凭据、工具与 Skill | 密码保存前取消零写入，PUT未知只查原键，重开不重发；普通持久存储／诊断无秘密；工具开关影响工作区同Agent、changed回执不丢其余行；测试明确Provider／次数且不自动触发；Skill原URI只读、远端Gateway投影不刷新，原Workspace刷新不替换封存上下文 |
| V5 现有业务直达 | 插件／MCP／Goal／终端／浏览器／端口／DAP／Diff／上下文从真实入口到原owner；工作区移除不删数据、共享Backend blocked不报成功或逐个重启；复用领域证据，新增接线核其命令owner |
| V6 六组可见 Thread＋按需管理 | Provider 页真实加载／关闭，Composer 可操作，不出现审批／全目标轮询；六组 owner、发送和资源关联不因管理切换改变；页面释放不停止业务 |
| V7 大日志与维护边界 | 十万条 Trace／大进程日志只读取原有界页／尾部；清 B 客户端缓存不删后端历史和 A 镜像；无新增全盘扫描或伪造总量 |
| V8 共享客户端与可发现性 | 中英文重载后命令／入口可用，键盘、Tab／Grid／dirty 恢复保持；复用干净打包产物核 Electron 原生启动及相同管理保存／重启归属，不用 electron-web 冒充 |

每个纵向切片仅为实际新增／改变的最多一个代表列表 API＋一个详情 API各取至少20个暖样本，保存原值与 nearest-rank p50／p95／max、选择依据和一条真实 UI 路径；首开／暖开分别记录，核响应身份、request_id、字节及 DOM／请求规模。纯接线可复用依赖源码未变的领域证据，相关依赖变化再补验；不把 V1–V8 乘全部页面／目标／语言／客户端。远端核路由与功能，WAN等待不硬套回环绝对预算，内部 Relay处理仍计入。新增入口都须确认接线，重写业务合同才扩大领域回归。测试必须有进程外超时／内存保护；正式产物沿测试路径，临时探索仅 `out/tests/temp/YYYY/MM/DD/<task_name>/`，不注册项目根工作区。


本轮9.3复核只读取现有正式原件，按原值重新计算 nearest-rank；没有重新请求页面或过滤慢样本。下表的六个21样本集合仅排除明示的首次API样本，保留全部20暖样本；V6的两个集合在页面先读取后直接采20暖样本，20条全部保留。160个暖样本的request_id均非空且组内唯一，原驱动核过响应头／体一致，字节均≤512KiB；全体p95／max通过本机预算。准确原件路径／SHA、逐条数值和规模在 `out/tests/temp/2026/11/13/harness_matrix_audit/artifacts/performance-recheck-02.json`；本轮保护命令、退出码及原始输出见同目录 `verification-01.json`，补齐2.1后的引用复核见 `verification-02.json`。这份机械复核收据不取代各run的源码／构建及原始命令。

| 切片／代表API与选择理由 | 正式run／20暖样本 p50／p95／max（ms） | 响应规模 | 同run真实UI可用（ms） |
|---|---|---|---|
| 凭据：`GET /api/gateway/device-connections`，原设备列表是该页代表读取 | `test_gateway_credentials_management/run-10c4edc622da`；4.60／8.40／15.30 | 97B；0设备，非大目录证明 | 首开853.25 |
| 连接：`GET /api/gateway/management/gateways`，本次新增管理目录 | `test_gateway_connection_management/run-4a8305092941`；4.70／6.50／7.30 | 606B；2目标 | 首开464.39 |
| Backend：`GET /api/gateway/workspaces`，原列表含默认健康检查；详情无新API | `test_managed_backend_runtime/run-9814276a9ad9`；72.90／89.40／92.10 | 2783B；2成员 | 首开132.58；工作区详情纯接线的另run首开149.50 |
| 诊断：`GET /api/gateway/diagnostics`，本次详情／observation消费者 | `test_gateway_runtime_observation/run-de69a4bbdc8a`；114.10／127.20／127.30 | 暖3508–6616B；1 Gateway的有界进程／运行摘要，不是全局总数 | 首开633.02 |
| 存储：同一diagnostics详情扩展owner位置，未增清理API | `test_gateway_runtime_observation/run-40cf7da37183`；100.20／129.70／133.90 | 暖3843–6940B；统计随请求增长，保留实际范围 | 首开621.52 |
| 实体／默认：`GET /api/v1/agents`，原默认回执及实体入口代表列表 | `test_configuration_entities/run-2f639fd63f70`；26.40／36.10／47.90 | 2722B；4 Agent | 首开681.47；历史browser exit0，外层pytest聚合收据缺失不补造 |
| 插件纯接线复用：`POST /api/gateway/service-plugins/inventory`，既有目录读取合同 | `test_harness_management_ui/run-325671635b33`；5.70／12.50／13.60 | 1248B；该fixture原目录，未记录全产品规模 | 原插件详情首开326.98 |
| Settings纯接线复用：`GET /api/v1/config/sources/workspace_root_mutable_override`，既有来源详情 | 同V6 run；80.00／87.00／296.50 | 120891B；同来源及schema投影，未新增摘要索引 | 该run未独立计来源页UI，不能拿插件详情计时替代 |

API首样本、API暖请求与UI暖开是三种数据，不能互相代替。工具列表／详情和Skill／MCP的本轮管理工作主要是接入原owner；没有改其列表／详情合同的页面不再各采20次。原模型测试20attempt是业务负载而非API暖样本，不据此凑性能统计；执行冷构造移至线程的修复也不改变这一点。

为补代表管理页的UI首开／暖开，沿原正式 `test_workspace_tool_management.py` 场景只增加计时，复用build10和原独占gate：初始化先真实完成 Gateway health／workspaces／workspace，再在 `boxteam.tools.focus` 前开始计时，直到同一workspace／Agent、真实工具目录200及request_id、schema和可操作控件均就绪；关闭原面板再打开，重新等同owner目录回读。`test_workspace_tool_management/run-a9cabe15c46b` 实测26工具，首开11074.14ms、暖开268.32ms：暖开通过，首开违反3000ms预算，当时9.3仍开放（后续修复见下文）。3个PATCH业务回执已完成、pageErrors为空、无模型测试POST；因为末尾首开预算断言失败，整体pytest exit1／65.71s，不能称本轮完整场景通过。准确保护命令、9份源码前后SHA一致、build10 main SHA和原输出在 `out/tests/temp/2026/11/13/harness_matrix_audit/artifacts/tools-ui-availability-01.json`／`.log`；真实截图／browser结果保留在该正式run。原日志显示agents已于03:53:06.898返回200，首次tools目录到03:53:17.575才返回，慢阶段由主线程继续在原ToolService／catalog定位；没有以预热、扩大期限或隐藏首开绕过预算。

工具首开后续修复已通过原完整场景 `run-52d75ec60736`（1 passed／84.06s）：26项工具首开825.18ms、关闭再开190.23ms，仍以同一 owner 的真实目录、参数 schema 和可操作控件为终点，保留原3000ms断言、三个单实体PATCH回执及零模型测试POST。源码改动仅将固定Agent执行依赖的加载放入原Workspace业务就绪前，原运行装配使用同一 `load_session_agent_factory()`；不构建模型／Thread／自定义工具，也不预填另一目录缓存。自定义factory仍在原目录／执行入口构建和报错，不因加速扩大启动失败范围。独立冷导入探针累计10.46s，实际本轮两后端的新增启动阶段分别1.153／1.073s，均由原启动日志单列；成本移至进程初始化，没有宣称消除SDK加载成本。原失败保留，不筛样本、不在测试提前调用工具目录。准确保护命令、9份源码前后SHA一致及build10指纹见 `GM/tool-catalog-startup-product-02.json`／`.log`；正式浏览器原件为 `out/tests/integration/clients/web/test_workspace_tool_management/runs/run-52d75ec60736/nodes/node-af6c72d4b504/artifacts/browser/result.json`。最终Python静态检查和9项runtime／工具定义缓存检查通过，另25项目录／策略检查沿依赖未变结果复用，收据为 `GM/tool-catalog-startup-check-02.json`、`tool-catalog-startup-check-01.json`。功能提交 `2b65e33f0`；本次只补该已失败场景，9.3代表API及UI预算现已完成。

9.4／V8 的Linux原生补验已接入正式 `test_native_management_closure.py`，最终 `run-b2933a974c08` 为1 passed／110.86s、exit0：真实Electron 45.1.0连续两次从源码目录外启动，使用同一ASAR、独立cwd／空PATH与隔离profile；原Provider管理表单编辑时零自动保存，显式保存仅一次PUT，dirty→clean，实际Gateway／Workspace owner、source_saved及request_id正确，目标工作区物理配置摘要吻合。两次正常关窗均exit0，重启后从同入口读回模型并核新的读取request_id，未重放写入。正式结果 `out/tests/integration/clients/electron/test_native_management_closure/runs/run-b2933a974c08/nodes/node-a9f97266e87d/artifacts/native-management.json` 的SHA256为 `f28ae5a27f28586919a988c451173482ce06f4f68bb9765a42e9c291364f5951`；保护命令、源码前后指纹和三个实际exit0见 `out/tests/temp/2026/11/14/management_native_closure/artifacts/formal-03.json` 及原始 `.log`。驱动复用既有 `launch.mjs` 的窗口、同来源重启和日志释放，未复制第二套生命周期；内层posix_job为240s／4GiB，外层900s包含600s共享闸门排队。原formal-01业务已完成但测试错误要求Workspace observation只能在关窗中止；原源码会在订阅变化时主动取消旧流，最终只允许该精确流的ERR_ABORTED，插件流仍要求正常关窗，其他页面／网络错误失败，原失败保留。

本次显式复用不可变development ASAR，SHA256 `0f4dd2893010e9ac08457f582b063f230de0489d54b4546986e60af8826d1c43`；构建提交 `352a7da6455042e34b13ff15209fb3d7fc131762` 与development用途见同临时目录 `electron-build.json`，未称重新构建当前全部源码。原临时native-16仅作先行证据，正式测试提交 `c14c6e3c1`。键盘／Grid／语言沿本节既有真实浏览器证据组合，不把该Linux产物当作Windows／macOS或完整发行9.8证明，未更新release基线或同步分支。

9.5 的 Quick Access 收口复用主 worktree 真实源码；受影响源码在 typecheck／build／定向测试前后与 `sources-before.sha256` 一致。生产 `ServicePluginPackagesView.openPlugin` 的无工作区边界短回归直接提取该方法验证：工作区目标在刷新后失效时明确报 `plugins.noWorkspaces` 且不打开编辑器，Gateway 目标仍可打开。`quick_access/plugin_management.test.mjs` 10项／52断言通过；新增 owner 回归1项／4断言通过。Workbench typecheck（198份转换源码）、完整 Workbench build 与 Web build 均通过。首轮 Web build 因输出目录末级名不符合 `dist` 校验在参数阶段退出1，保留原日志后修正目录并通过，非构建失败。命令、退出码、原始日志 SHA、15项源码指纹及native原件复审记录见 `out/tests/temp/2026/11/13/contribution_closure_luna_max/artifacts/quick-access-cleanup-001/closure-receipt.json`；Quick Access正式产物为 `out/tests/integration/clients/web/quick_access/plugin_management.test/runs/run-6f30a1bfee6e/artifacts/plugin-management.mjs`。Linux原生管理正式 run 只读复核为1 passed／110.86s、两次正常退出、显式保存一次PUT且重启后同 owner 读取成功；测试复用既有 `launch.mjs`。该run原件、源码指纹与SHA记录于同收据；这些证据不代表Windows／macOS或未重建的当前发行ASAR。

9.6的矩阵复核保留以下交付边界：V1新启动双Gateway已由2.1的 `test_service_plugin_target_gateway_ui/run-81962f7a3466` 专项验证，SSH执行／隧道仍为loopback替身；V3共享／继承／引用组合已由4.7及各原owner证据闭合；工具／Skill的V4剩余合同由5.5核实；V5代表入口由7.7汇总，Goal新页仅覆盖R03／R04所列转移，Ports另由真实localhost SSH代表场景闭合，不属于原20个运行资源flag，后续改变的DAP仍未完整重验；V8已补Linux原生独立产物验收。跨机器SSH、Windows／macOS、Profiles完整导入导出及未提供能力仍未被这些Linux Web历史run证明。其它已通过API不因矩阵页数或语言组合重跑。所有历史收据只沿其自身源码／产物边界复用，当前UUID／协议／其它并行变更未由旧run自动验收。

3.5／8.1／8.2 的新增诊断链已通过真实 Gateway／Backend／Workbench：沿原 Gateway Settings 的同一 JSONC 模型、预检和 CAS 启用遥测／统计，诊断详情消费原 observation 回执，默认摘要只复制显式身份／状态白名单；原始日志保留用户单独复制入口。关闭页面后停止读取，范围切换／关闭取消本页请求，HTTP 错误保留原 request_id。原 About 显示根 package.json 的 BoxTeam 版本和构建提交／提交日期，固定上游版本核验及内置扩展 API 版本保持独立。`tests/integration/clients/web/test_gateway_runtime_observation.py` 1 passed，69.90s；首次真实可用633.02ms，唯一代表诊断详情21样本中20暖样本p50／p95／max为114.10／127.20／127.30ms，未发模型或审批请求。正式原件在 `out/tests/integration/clients/web/test_gateway_runtime_observation/runs/run-de69a4bbdc8a/nodes/node-4b6cfb820641/artifacts/`；命令、前后源码指纹及exit0在 `out/tests/temp/2026/11/13/gateway_management/artifacts/diagnostic-observation-product-01.json`。同批类型检查07／Web构建05通过，唯一构建期间源码变化为目录观察流的两行TODO注释；固定产物main SHA256为 `1b1971e7c24c2cf803b73f6039aecdb7b01e7fdc9f7fad7bf9c69843ac0f3768`。组件／API定向25项及GCC／日志定位6项通过。这些证据不替代大型Trace、所有远端错误路径或原生Electron验收。

8.3 已沿原回复与 Goal 投影补齐用量边界：回复显示最后一次模型请求并明确不是 Thread 总计，Goal 只报告自身已记录用量；缺少用量或已确认价格显示未知，不扫描历史或新增计价／聚合。`ComposerLocalization`、`GoalManagerCard` 和 `tokenUsage` 共13项通过；同批实际类型检查08与 Web 构建06通过。边界本地提交 `c98d82def`，收据为 `out/tests/temp/2026/11/13/gateway_management/artifacts/usage-presentation-01.json`。

8.4 的存储位置与保留入口已迁齐 proto、生成结构、原 owner 和 UI：Gateway 诊断提供目标机器的控制面／日志位置，Workspace 详情只展示所属 owner 的业务数据位置；来源位置按钮按需读取原目录元数据，逻辑来源没有文件 URI 时明确显示未提供。日志保留直达同一 Gateway 的原 Settings，不新增清理、备份或修复接口。`storage-product-01` 整体 exit1：双 Gateway 工作区节点通过，诊断节点因测试错误要求 SQLite 逻辑来源必须有文件 URI 而失败；原失败保留。修正该预期后，`storage-product-02` 单独诊断／来源／About 流程通过（exit0，34.04s）。诊断20暖样本 p50／p95／max 为100.20／129.70／133.90ms。两项通过节点的正式结果分别位于 `test_workspace_details_management/runs/run-1befb76565a1` 与 `test_gateway_runtime_observation/runs/run-40cf7da37183`，均无页面错误或禁止请求。类型检查10、Web构建07、协议生成一致性／breaking检查、Python47项及诊断组件10项通过；固定构建main SHA256为 `3a24bc9142a43ee14a6ad89709f0f1c55197e474aecae73618cfddcfa61f02aa`。产品测试期间其它 Agent 修改了 theme 和 Thread 打开源码，测试使用的冻结构建及本切片源码未变；具体前后指纹和退出码在 `out/tests/temp/2026/11/13/gateway_management/artifacts/storage-product-01.json`、`storage-product-02.json`，不据此宣称其它在途改动已验收。客户端缓存精准清理复用前述依赖未变证据；8.5 的十万条 Trace 证据及适用边界见下文。

8.5／V7 已验收：`tests/unit/api/test_session_trace_pages.py` 用隔离 Session Trace SQLite fixture 准备1个真实 Job owner 和99,999条 schema 序列化的合法事件，经真实 FastAPI `/api/v1/sessions/{session_id}/traces` 请求读取最新页和游标续页；两页身份、顺序、Session／main Thread 归属、前进游标、`has_more`、request_id 均符合合同，每个三项响应小于16 KiB。进程日志沿原 `app.gateway.diagnostics._read_log` 尾读 owner 验证：超过1 MiB 的日志只 seek 到末尾并读取256 KiB，按请求保留三行并准确标记截断。Gateway 安全诊断摘要白名单测试继续断言模型正文、API key、token、错误正文和本机路径均不会进入复制结果；真实 Thread 缓存清理复用 Quick索引6的 `ui:protected_recovery`，其收据 `out/tests/temp/2026/11/12/thread_cache_native/artifacts/verification.json` 记录 Web 与 Linux development ASAR 两项通过，正式 Web 为 `quick_access/test_recent_history/run-133b7b960fa4`：清除 B 精确 Thread 后其它 Thread 缓存、后端历史与草稿保留。旧 owner 收据中的 `run-72f8a8b0ff83` 原件无法定位，不再作为 UI 完成证据；owner 的单测及指纹证据仍可单独复用。本次两个 Python 用例分别 1 passed（HTTP Trace 5.20s；Gateway 尾读0.89s）；组件复验 `bun test ...GatewayDiagnosticsPanel.test.tsx ...threadHistoryCache.test.ts` 为26 pass、0 fail。准确命令、原始输出及源码 SHA-256 保存在 `out/tests/temp/2026/11/13/contribution_closure_luna_max/artifacts/8-5-v7-http-trace-receipt.json`、`8-5-v7-gateway-log-receipt.json`、`8-5-v7-owner-receipt-v2.json` 与对应 pytest／Bun 日志；生产读取 owner 均未改动。无全盘扫描、伪造总量或新增持久状态。上述为 owner/API 与既有 UI 清理证据，不代表所有远端路径或原生 Electron 验收。

第6组／V5 插件与 MCP 新增管理接线已完成真实 owner 验收：`tests/integration/clients/web/test_mcp_plugin_configuration_ui.py::test_add_plugin_service_and_http_from_original_workbench` 的 workspace／gateway 两范围最终 2 passed／164.90s（runner168.51s，exit0），正式原件在 `out/tests/integration/clients/web/test_mcp_plugin_configuration_ui/runs/run-03f2e5eec5d1/`。原 Server Editor 的参数按钮直接取得 `boxteam.mcp-stdio/stdio@0.1.0` schema，两范围分别带／不带实例 workspace，Gateway HTTP 的合法 `workspace_id:null` 仅在同一命令交接边界归一；原详情与 Options 的来源／参数入口共用同一实现，取消不写。资源 Quick Access 齿轮沿原 Quick Input controller 直接替换浮层，删除先隐藏再微任务打开造成的 Webview 焦点竞争；每次真实点击只提交一个原连接操作，disconnect／connect 各一次 succeeded。原 MCP Readme 明确 resources/templates 浏览未提供，连接动作不重启或停用插件实例。

自有下载源从插件原范围菜单进入同一 Gateway 的 Settings；真实表单／JSONC 共用原来源模型，经预检／确认／CAS 保存 `gateway_mutable_override`，回读 JSONC 与 source_effective 一致。两范围各三次 PUT 的 owner 为工作区两次及 Gateway 一次，最后一项不携带 workspace；空源安装沿原 inventory 回执说明无启用源，零 source-catalog 和新增外网请求，两套 MCP fixture 均无业务工具回执。原插件包 scope／安装更新卸载／默认值证据复用 `out/tests/temp/2026/11/12/plugin_package_scope/artifacts/verification.json`（正式1 passed／290.28s），实例日志复用 `out/tests/temp/2026/11/10/plugin_instance_logs/artifacts/verification.json`；本次日志读取只补同一取消信号及迟到拒交，不建新日志或监督 owner。103项领域定向检查／698断言、真实198份转换源码类型检查及正式 Web build10 通过，固定 main SHA256 `4807dc8818c4bae4b71f98528e563c5b1c1bf7ed7491475387291745383e1390`。准确命令、前后源码／构建指纹、正式结果 SHA 和失败归因见 `out/tests/temp/2026/11/13/gateway_management/artifacts/plugin-mcp-management-acceptance-07.json`、`plugin-mcp-product-07.json`；13份本切片源码及三个配置 owner 前后不变，测试期间仅另一场景两个驱动文件变化。此前 Bun JSC SIGILL 后驱动单一改用现有 Node；第06轮暴露的 null 边界及动态行裸 CSS 快照已分别窄修和删除，原失败保留。本轮为本地 Linux Chromium 的接线验收，未替代真实 SSH、跨 Gateway 完整矩阵、Windows、Electron 原生、OAuth及秘密提交领域生命周期；没有新增 API 或重写领域性能合同，不重新采样所有旧接口。

## Risks / Trade-offs

- 清单很广，容易演变成每项新写一页 → 按 D4 的已有／补齐／受限交付；先找消费者，复用原页与单一 writer，任务按业务纵向切片。
- 默认本地被错误理解为远端 Tab 也改本地 → 入口选择与输入 owner 分离，V1／V2 直接验证。
- Workspace 用户来源被当成每进程默认 → 由正式来源说明共享和 fan-out，V3 核两个真实消费者；不新增配置域。
- 新增 CRUD 扩大权限或泄漏秘密 → 保持原目标授权、原 owner 引用检查和秘密通道，跨层变更先协议／类型，再核序列化。
- 大列表、统计和诊断带来全历史读取 → 只使用已有有界摘要，缺失统计显示未知；不为本页新建索引／后台采集。
- 原生控件存在被误称已完成产品能力 → 按源码接线和真实入口分别登记证据；受限能力说明不能替代承诺实现的实体管理。

## Migration Plan

实施按独立纵向切片推进，不以规划授权变更用户运行环境：每条链先检索声明、writer、直接／间接消费者和测试，再统一迁齐并删除失效入口、旧 demo、死组件及测试；没有调用方的旧 UI 不保留兼容开关。复杂度复审删除重复状态、包装器、轮询、校验和无业务需求的持久化。

沿现有配置层、引用和 URI，不因页面重组迁移或复制用户数据。没有已验收 release 基线不自动补开发中间态迁移；必要新增字段／接口在原 owner 明确版本和全部消费者，不把本计划作为批量兼容授权。失败保留原 active、草稿及真实结果，依原合同显式处置，不新增自动回滚链。
