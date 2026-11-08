# 目录用途

`gateway_auth_resource_recovery.mjs` 同时核验导航错误的原版宿主出口：只将导航 GET 替换为明确的 503，打开真实诊断对话框，检查完整多行正文按文本呈现、无需再次读取服务日志、草稿保留，以及撤销替身后的真实 Gateway 恢复及 request_id。该失败替身必须写入结果边界，不能表述为真实服务故障验收。

存放纯 Web 组件/页面 harness，或浏览器前端、Gateway、后端和受控替身服务之间的集成测试。

按测试实际加载的边界组织：

```text
web/
├── components/        独立挂载组件的 Vite/Chromium 装具及其生命周期检查
├── itemized_context/  条目化上下文的页面场景与受控 Provider
├── workspace_files/   文件 owner、浏览器意图与最小 HTTP 支撑
└── *.py / *.mjs       其余 Gateway/工作区/完整页面组合场景
```

组件脚本、HTML、TSX、类型配置和专用 Python 测试相邻放在`components/`，详见该目录的`AGENTS.md`。加载完整产品页面的场景不因使用Playwright而归入组件目录；只有出现共同职责后才继续分组，不按文件数机械拆分。新运行的正式产物镜像当前测试路径，历史结果保留原路径，不建立旧源码入口或产物路径回退。

`session_catalog_outbox_idb.mjs`使用同一运行上下文管理静态HTTP监听、Chromium和context，取得句柄后立即登记，再开始下一准备步骤。任何清理失败仍尝试其余资源；执行和清理同时失败时合并保留两个原因。原生临时文件归该脚本的`runtime/`，不与IndexedDB业务事实混为一层；跨标签页持久化场景及原断言不由生命周期装具替代。真实故障回收检查归`tests/integration/harness/test_outbox_fixture_lifetime.py`。

`manual_browser_connection.mjs`的资源作用域从Chromium启动前开始，取得browser/context后立即向同一运行上下文登记关闭，再继续创建page和注册路由。准备及场景失败都释放已有资源，context关闭失败仍尝试关闭browser；执行与清理异常并存时同时保留。页面尚未取得时不采集截图，已取得页面的原诊断及业务断言保持。临时文件归该脚本的正式`runtime/`，生命周期故障检查归`tests/integration/harness/test_manual_browser_fixture_lifetime.py`，不替代完整Gateway连接场景。

`boundary_cases_display.mjs`、`partial_text_boundary_display.mjs`和`unknown_tool_outcome_display.mjs`负责历史边界的页面展示；各自持有Chromium、context及页面，复用运行上下文登记逆序释放。准备阶段也在释放作用域内，关闭一个资源失败仍关闭其余资源，执行和清理异常共同保留；截图仅在取得页面后采集。原生临时文件归各脚本的正式`runtime/`。三者共同的真实浏览器故障回收检查归`tests/integration/harness/test_boundary_display_fixture_lifetime.py`，页面业务断言仍由原场景负责，不新增一层浏览器装具。

## 可修改内容

- 浏览器性能、会话历史和网络交互集成测试。
- 真实浏览器加载局部组件 harness 的集成测试。
- 测试专用 Playwright 驱动脚本。
- `workspace_files/` 下工作区文件操作的真实 owner、持久浏览器意图与最小 HTTP 支撑；保持为明确受控边界的 Integration。

## 不可修改内容

- 没有受控服务替身的完整 Web 产品 E2E。

## 规范

- `target_tabs_product.mjs` 经正式 Workbench、双 Gateway／Backend 和两个真实 Node 宿主检查公开 TabGroups 投影、身份、MultiDiff、移动及关闭；页面与 Chromium 关闭后继续核原管理服务身份及显式 Relay 库存归属。SSH 仍是具名 loopback 装具；未开放的 `$moveTab` 与 `goToFileUri` 只由定向转换测试覆盖，不冒充公开产品入口。
- 完整产品页面场景必须通过真实 Gateway 访问；局部组件 harness 必须明确标识为 Integration。
- 完整 Workbench 页面使用 Gateway 默认根 URL，不依赖测试专用的 Agent 查询参数；独立 MCP 授权仍使用既有授权路由。
- 服务替身和页面路由拦截必须显式记录，不能伪造成功状态。
- 运行产物必须写入 `out/tests/integration/clients/web/`。
- `test_basic_chat_tool_loop.py` 使用 `web_workbench_assets` 构建完整 Workbench，由真实 Gateway 提供静态资源；浏览器从会话树及资源面板打开 main/child，按原版编辑器组和精确 owner 定位 ShadowRoot 内控件。模型使用显式 model_stream 回放，因此属于 Integration；保留工具循环封存请求和双 Thread 并行输入隔离断言。
  - 活动选择消费公开 `active_workspace`，与 health 的 Gateway 身份及工作区持久 UUID 交叉核验；业务请求同时携带 `X-BoxTeam-Target-Gateway-Id` 与同一 UUID。工作区及会话目录节点使用完整 owner 的复合键，证据不再保存路由／业务双 ID；原准备失败装具仍在业务访问之前触发。
  - 标签生命周期通过真实单击／双击核对预览替换与固定，原版关闭按钮按 Tab 内的可访问角色及名称定位，不依赖上游图标类；关闭后核后端会话仍存在并可从导航重开。分屏先验证同一 Thread 两组的资源展开状态独立，再验证 main／child 草稿、模型并行与回复隔离；新增检查不替代原封存请求、真实文件写入和磁盘哈希断言。
  - 删除刷新回归先真实保存同一 Session 的两个分屏标签，再执行真实目录删除并核 committed／settled、标签及宿主释放；随后立即刷新，不提前 flush、不额外等待保存，要求保留会话可操作且不重新读取已删除 Session。
  - 标签生命周期还延迟精确工作区／Session 的真实 main-thread GET，随后点击另一原版 Tab；放行真实响应并等待目录请求完成后，活动 Tab 必须保持用户的新选择。只用 route.continue，不伪造响应，核实际 owner 与 request_id；不能靠额外点击或延长期限掩盖迟到导航抢焦点。
  - Chromium 准备和场景执行使用同一资源作用域，取得 browser/context 后立即登记逆序释放；执行、诊断保存和清理同时失败时保留全部原因。失败 JSON 记录真实阶段、已取得的 owner、acceptance、SSE 和页面错误，不能填入未执行步骤的成功结果；仅在取得 page 后截图，诊断保存失败也必须显式报告。真实 Chromium 故障回收检查复用 `tests/integration/harness/test_boundary_display_fixture_lifetime.py`，不代替完整聊天验收，也不改变原业务等待期限。
  - 轮次错误通道通过真实消息菜单进入编辑，只对精确 owner 的一个 replay POST 声明显式 409 HTTP 替身；核错误可见、编辑与下一条草稿保留、聊天可操作，以及经真实 Gateway 返回的历史 owner／request_id／原 Turn 和投影 epoch。撤销替身后继续双 Thread 并行发送及原文件保存断言，原四次封存模型请求不变；不能将此拒绝替身称为后端 replay 事务失败验收。
  - 双 Thread 模型屏障持有期间，仅精确 main Thread 的 interrupt POST 使用显式409替身；失败后必须有真实队列200重读和request_id，核原job仍活动、所属Composer错误可见、另一Thread无该错误、下一条草稿保留，随后两侧原模型调用继续完成。不将该替身称为后端停止事务失败验收。
  - 会话右键复制失败只替换浏览器Clipboard API及既有兼容复制为显式拒绝；从真实会话树打开菜单，核原版通知完整错误及发起Gateway／工作区／Session身份、单次调用与输入草稿保持，随后恢复原浏览器方法并继续原聊天断言。没有业务HTTP替身，不能将浏览器能力拒绝称为系统剪贴板权限验收。

- 历史边界及元数据初始化页面也使用正式 Workbench 产物；Thread 内正文通过可穿透 ShadowRoot 的作用域 locator 读取，不能以 document 全局查询的空结果证明成功。元数据在打开对应 Thread 后核对；边界状态、工具顺序及清理失败断言保持。
  - 元数据场景同时覆盖 Gateway 静态产物和真实 Workbench Vite 源码入口；业务工作区与未注册目录按 run／测试节点隔离，两个参数不能共用目录。Vite 与 Gateway 在本测试获分配的端口块内使用 E2EProcessLease 选择并持续保留空闲绑定，服务关闭后再释放，将探测写入同一 port-allocation.json，不终止未知进程或更改宿主端口设置。Vite 变体显式指定本用例 runtime 的静态资源输出及派生缓存，核 JSON health、Vite 客户端、源码首页和三条真实 Gateway 初始化接口，再复用相同的浏览器文件／设置／刷新／日志持久化断言。Vite 子进程组取得后立即登记回收，不允许以静态首页回退掩盖开发入口失败。
  - 元数据场景还通过原版工作区设置编辑器连续三次保存并直接刷新，核对 JSON 与实际编辑器字体恢复；不预写 IndexedDB，不提前调用关闭 flush。设置可能使用原版 modal editor，按对话框与可见代码行定位；存储关闭错误仍须审计，不能由已知原版关闭日志豁免。
  - Vite 场景直接启动产品 dev 命令指定的 Node／scripts/dev.mjs，让 PID 监听探针观察实际服务 owner；不把 bun run 包装进程冒充监听者，也不移除监听归属检查或放宽原期限。
  - 主题迁移通过真实 Gateway 控制中心保存网络背景，使用隔离 Gateway 中不存在的合法资源摘要取得原始 404／request_id，核对原版通知 toast 和导航／精确 Thread 宿主的无图片主题；恢复默认背景后聊天仍可操作。没有图片 HTTP 替身或数据库预写，组件 collector 不能代替此接线验收。
  - 元数据 Chromium／context 取得后立即交给既有 TestRunContext；执行、截图、结果写入和清理失败须同时保留，截图失败不能阻止 JSON 落盘或浏览器关闭。失败 JSON 包含未完成资源请求及原始页面／console 错误；准备和诊断故障验收复用 test_boundary_display_fixture_lifetime.py，不替代真实产品页面。
  - 元数据场景末尾显式合成 beforeunload/pagehide，使原文档继续存活到延迟日志写入完成；只读真实日志 store，核关闭日志计数增加且无存储错误。该受控检查用于稳定暴露提前关闭连接，不替代前面的真实刷新，也不宣称浏览器强杀时日志必然落盘。
- MCP 授权、连接错误、工具审批及远端授权场景使用同一 Workbench 构建装具；底部原 `McpServersListView`、`McpServerEditor`、列表命令与授权链接打开的完整 Workbench 都经过真实 Gateway，共用项目 MCP owner。详情正文从实际双层 Webview 读取，未接入的原配置／凭据命令须明确拒绝。Thread 和请求按原生 Gateway／workspace UUID 核对，Relay 身份与操作目标分开；面板恢复使用原工作区展示态，保留回调秘密不泄漏、原 300 秒期限和封存请求的强断言。不得用旧 React 列表或手填连接事实替代原控件验收。
  - 授权管理窗口的目录首次加载使用明确的 503 响应替身，直到原列表实际展示错误；随后沿真实 Gateway 自动恢复同一列表并清除面板错误，再继续真实 OAuth、目录与工具调用。该检查证明原控件失败恢复，不称为真实服务离线验收。
  - MCP 专项在打开 Thread 前，经真实目标的 `/api/v1/agents` 等待 SDK 配置检查就绪并保存耗时／request_id；不改可用性语义，也不将它视作冷启动 Thread 的性能验收。首次 SDK 导入导致的冷初始化超时仍须单独处理。
- 附件目录页面使用正式 Workbench，按精确 Session/Thread 进入附件预览及派生文件编辑；刷新后核对浏览器恢复的文件预览及真实文件 owner 的冷读。会话快捷目录仍由后端保存，原件不变和快捷目录不扩散的断言保持，不再等待旧服务端布局写入。
- Backend 完整组管理从主侧栏进入 Gateway 控制中心，确认操作定位独立的业务确认对话框；公开错误与原始诊断按同一 request_id 核对。手动浏览器从所属 Thread 的运行与连接创建，在该 Thread 内预览，不等待旧扩展窗口。
- 认证恢复和渐进历史场景加载正式 Workbench。资源页验证真实 401 恢复、可见性轮询及窄宽布局；历史滚动先确认精确 owner 和正文节点，再测分页、工具详情与渲染耗时，不能用全局 document 查询的空结果证明成功。
- `navigation_mutation_measurements` 是显式独立组件量化 harness，保留其受控挂载边界；不因完整页面改用 ShadowRoot 就机械改写该 harness 的 document 查询。
- 子 Thread 委派场景使用明确的模型回放与真实 Gateway/Agent/task 工具，属于 Integration；工作台按精确 main/child owner 操作，保留原三次父调用与一次子调用，并额外核验当前自动回报父 Thread 的一次内部调用（合计五次）。子请求必须包含完整冻结任务来源，父回报必须包含真实子结果；保留 SSE 断言，并验证首次与复用标签都展开 child 调试资源。
- `test_browser_view_sessions.py` 加载正式 Workbench，用真实滚轮和控件产生 IndexedDB 展示态，验证刷新及同浏览器新页面恢复、独立 BrowserContext 隔离、草稿隔离和未打开会话的真实未读通知。旧账户菜单已删除，账户占用／接管／真实过期重取只通过 HTTP 核原合同；不再期望另一浏览器自动继承视图，不预写数据库，不向服务端写展示态。历史装具与受控模型回放必须明确保留 Integration 边界。
  - 未读验收先在两个独立 BrowserContext 观察真实后台完成提示，再聚焦其中一个页面，延迟精确工作区／Session 的真实 main-thread GET。树行打开中会以加载图标替换活动指示，必须通过真实搜索中的同一会话确认未读保留，核搜索回执的精确 owner／request_id，不能用加载图标或不可见节点证明未读状态。放行真实查询并核 Session／Thread／request_id，打开精确 Thread，核正文和输入可用后搜索结果与树行提示均消失；另一页面仍保持未读。不直接改 React 状态、不伪造活动事件。
  - 历史缓存验收在新页面导航前延迟精确 owner 的真实 Thread bootstrap GET，只使用 `route.continue()`；同浏览器重开先显示已持久化的只读历史、没有输入框，独立 BrowserContext 先显示加载状态且无缓存正文（输入仍沿用既有可用行为）。放行后核真实响应 owner／request_id、缓存退出与输入恢复；不得以预写 IndexedDB、伪造 HTTP 响应或组件单测代替。释放延迟点后等待路由回调结束，再关闭页面。

- `test_persistent_resource_lifecycle.py` 使用正式 Workbench 和显式精确模型回放；真实 Terminal/Browser 经 Gateway 管理。`unload-restart` 参数场景从 Thread 内创建，到工作区底部面板管理解除关联的保留资源，保留真实 PTY、Browser 导航、安全重启、跨 Thread 关联、确认期间并发修改的 409 与失败重读、删除来源 Session 后仍可操作的断言。`workspace-browser` 参数场景独立验证工作区 Browser 按当前 Thread 创建、预览、解除关联及删除，核对请求 owner、request_id 和确认前 revision；它不替代重启场景，也不修改退出期限。

完整会话生命周期与 Section 8 唯一验收 owner 位于 `conversation_lifecycle/`；精确模型回放属于 Integration，复用正式 Workbench，不保留旧 E2E 入口。

`workbench_server.test.mjs` 在独立 Node 中调用 Electron 与独立静态服务共用的真实 HTTP 代理，Gateway 使用随机本地端口替身；核对 SSE 客户端关闭、响应头返回前取消、另一连接继续收事件，以及 JSON 正文／owner／cookie／request_id 透明传输。释放观察发生在 fixture 强制清理之前，原始 stdout、stderr、命令和退出结果归本测试的正式镜像目录。只关闭自己创建的连接；不启动产品 Gateway，不将此传输边界检查作为完整聊天、WebSocket 或原生窗口验收。Node 只停用 http-proxy 已知的 DEP0060 弃用告警，其余 stderr 必须为空。

`test_thread_editor_identity.py` 经正式 Workbench、真实 Gateway 和托管工作区创建 Session，再通过原版打开命令与会话导航核对稳定 Thread 身份、标签去重、分屏及刷新恢复。只有歧义场景向真实登记列表复制一条明确替身记录；未知 Gateway 与歧义均不得读取对应业务 Session。替身卸载等待在途回调结束，避免清理阶段继续处理已释放的 route；保留 request_id、页面错误、原始输出和失败截图。原版关闭空组后在当前组重开已存在资源时复用标签，不要求另建重复标签；该检查不能代替真实远端重连或 Electron 原生恢复验收。
