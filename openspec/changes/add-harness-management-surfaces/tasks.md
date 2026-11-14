## 当前优先切片：已确认的正式前端交互

- [x] F1 扩展侧栏联动 Gateway／工作区下拉，首项默认及按 Gateway 记忆；范围与安装进入原菜单，不弹重复工作区选择。
- [x] F2 插件图标／名称／简介／状态及原生行菜单；迁出侧栏详情至固定 owner EditorPane，复用单一配置操作、预览／固定、Grid 及恢复。
- [x] F3 原 Settings 暴露模型与 Provider 入口，继续使用同一来源 Settings／JSONC 模型和显式保存；保留搜索、管理菜单及 Ctrl+,。
- [x] F4 真实类型检查、Web 构建和定向 Gateway／Backend／浏览器验证，核侧栏启停、Tab 归属、窄窗及零审批轮询；最小实现复审。
- [x] F5 用原 WorkbenchList／InputBox、ActionBar 内的 SelectActionViewItem 和固定 upstream 扩展样式替换自绘列表及详情，保留包分组与原业务操作；目标选择器沿用原工具栏槽位、下拉菜单和禁用状态。真实浏览器核宽／窄布局、原生样式生效、正文导航和既有交互并留图，不以交互通过冒充视觉一致。

本轮验收：`tests/integration/clients/web/test_harness_management_ui.py` 通过真实本地 Gateway／Backend 和完整 Workbench 验证上述交互；30 项插件 owner 定向测试通过。远端多目标完整矩阵、Electron 原生窗口及以下完整领域计划未由本轮验收替代。

F5 复验使用 `run-b89a840e80ce`，原扩展 72px 列表行、128px 图标、26px 标题及主次操作样式实际生效；宽／窄截图与配置读写、正文导航、Grid、重载恢复均经同一正式产品入口验证。较窄编辑器仍沿用原版页头，名称和版本可能被裁切；本轮未另造响应式外壳。

顶部目标工具栏后续复验使用 `run-c978e5109caa`：两个原生选择器实际高度 21px，左右内边距 8px／23px，原下拉展开与 Esc 关闭、Gateway 范围禁用工作区选择均通过；同批类型检查及完整 Web 构建通过。右下角原生键盘布局指示保留。

以下完整领域计划保留；本切片不代表 Provider／Agent 的全部 CRUD、秘密编辑和所有管理面矩阵均已交付。

按下列勾选记录实际完成状态。分组表达依赖及业务切片，不规定无关领域的串行顺序。第1组确定真实合同，第2组提供必要入口；后续领域只等待自己的上游。每个切片迁齐声明、权威写入方、消费者和测试后集中验证，再删除旧入口，不先造空壳或一套通用管理框架。

矩阵 ID 见 design.md D4；“受限”项只交付准确支持说明与已有操作，不据此新增未约定业务。已存在能力以接线验收交付，不重新实现。

## 1. 确认权威接口和接入边界

- [x] 1.1 逐项核对 C／T／S／A／P／R／O 矩阵的实际注册、消费者、owner 和相关测试；在本 design 原矩阵更新接线证据，不另建生产 capability registry。
- [x] 1.2 核对关联活跃变更已落地的接口与职责，给需要补齐的 Provider／Agent／Skill 操作标出原 owner；不重复实现插件、搜索、缓存或同步。
- [x] 1.3 对本切片必要协议变化先迁齐 proto、生成 DTO、writer、直接及间接消费者；不预建尚无消费者的接口、适配层或兼容分支。

## 2. 默认本地目标与统一可发现入口

- [x] 2.1 扩展现有管理目标的入口 scope，初次／新启动默认本地、显式远端选择互不干扰；Provider／Agent／工具等全局入口在所选 Gateway 下首次默认首个工作区，切回恢复本入口上次选择，直接下拉切换；不借焦点／activeId，不自动激活或创建；SSH转发不改“本地”归属，覆盖V1（T01、S01–S03）。真实新启动 Gateway A 的 Agent Settings 经原来源读取，显式切换远端 Gateway B 并核真实请求 owner，切回 Gateway A 后重新打开同入口仍读 A，活动工作区不变；已通过 `test_service_plugin_target_gateway_ui.py::test_workbench_settings_selects_remote_gateway_and_returns_to_local`。
- [x] 2.2 把 Gateway、共享 Workspace 用户来源、工作区和上下文对象的范围说明接入原选择与详情；fan-out 标明仅覆盖原 owner 已观察消费者，停机／未知不伪报已应用；不新增 Backend 配置层（T01、S02–S03）。
- [x] 2.3 为新增管理详情接入原编辑器身份、标题、dirty、分屏、恢复和失效处理；覆盖双目标同名对象、B 草稿保存不投 A；共享源按工作区保留基线／回执，一个保存后另一个冲突保留草稿；沿原配置能力准入，冻结在途路由，失效不回退或重放 V2。
- [x] 2.4 在原 SessionNavigation 节点接入原管理命令、菜单和欢迎直达，复用导航／Quick Access；对象入口显式传owner并在首个await前捕获，ContextKey只负责作用域内显隐／可用性，直接命令仍核参数及准入，不逐字段建命令。打开详情不激活工作区；无Session可见入口，Backend未就绪显示原错误与显式操作，不造空列表、第二导航或activeWorkspace（C01、T01、A01、A03、A05–A07）。
- [x] 2.5 将外观和客户端偏好暴露在管理菜单／原 Settings，核快捷键、语言、Profiles 实际支持及中英文文本；不恢复用户档案或云同步（C02–C06）。

## 3. 目标与连接管理切片

- [x] 3.1 复用导航和 Gateway 控制中心完成目标详情、连接已有创建／编辑／移除／重连操作的直达与结果重读，保持 config／manual owner 区别（T01–T02）。
- [x] 3.2 补工作区详情与已有创建／注册／重命名／移除／打开入口，使用目标机器原root binding；移除注册不删磁盘数据／会话或自动重注册，连接config／manual归属不混写，核远端权限错误（T03）。
- [x] 3.3 完善共享Backend运行详情与生命周期影响说明，核generation／完整成员、原安全／强制操作及blocked回执；同进程不逐工作区循环控制，冲突不自动强制、不伪报成功（T04）。
- [x] 3.4 将设备／入站访问与凭据已有操作接入可用入口，替换“连接与凭据”禁用占位；不足能力准确说明，不另建凭据库或登录服务（T05–T06）。
- [x] 3.5 接入目标健康与故障的配置／日志定位，错误保留原 request_id 和 owner，不自动启动／重连／修复（T07、O05）。

## 4. 正式设置与 Provider／Agent 管理切片

- [x] 4.1 统一设置标题、来源／继承及保存／导入／生效／待重启，表单与JSONC共用原模型；业务source仅显式预检／确认／CAS保存、客户端原生Settings保持原行为，即时业务开关沿其owner；清理未接入字段误导控件（S01–S05）。
- [x] 4.2 在原配置 owner 完成 Provider 必要创建／编辑／移除本层定义及当前引用校验；在原 restore／preflight 内仅为 api_key 原占位核同一来源旧项、唯一 ID＋endpoint 后恢复至新索引，其他秘密路径保持原约束；同一 writer／来源 CAS 提交，无 CRUD 专用事务／协议／秘密表；保持最小参数、环境引用，不支持 ID 重命名（A01）。
- [x] 4.3 接入 Provider 列表／详情并直接编辑原来源 JSONC 模型，表单无第二草稿；定义读取不依赖成功展开全部凭据。新增高层数组明确整组覆盖／被遮蔽对象，恢复继承移除整组覆盖；只改所选层，不复制脱敏 effective 或引入隐式物化；与会话模型选择器互相直达（A01、A04）。
- [x] 4.4 接入 Provider 非持久化密码输入：结清原脏来源，沿同一 source writer 预检／CAS 写 api_key；保存前取消零提交，发出后结果未知沿原键及 edit 回执查询，不比较明文与脱敏 JSONC、不重发或另存秘密摘要；恢复只保留非秘密查询身份。核同绑定占位、换身份／地址拒借及已有登录／显式诊断，不建秘密 owner 或新增 OAuth Provider（A02）。
- [x] 4.5 在原 Agent／配置 owner 完成定义创建／编辑／移除本层定义，明确下层同 ID 恢复，无 tombstone／跨层删除／ID 重命名；Provider／Agent 引用只查完整 candidate 当前引用与该工作区默认值，不扫描历史或全 catalog、不建跨 owner 事务，历史缺失显式报错而不改绑；保留 Job 快照（A03）。
- [x] 4.6 接入 Agent 列表／详情、提示词／Provider 引用、默认值说明：定义及 default_agent 走原 source writer，工作区会话默认走 SessionDefaults，当前选择走 Session owner；与 Composer 共用各自原操作，不迁移、双写或统一成新 writer（A03–A04）。
- [x] 4.7 完成 V3：两工作区共享来源已观察结果＋私有覆盖、未知不伪报完成；同模型保存重读、整组覆盖／继承、Agent 下层恢复；primary／fallback／默认值冲突与无历史扫描，配置默认不重绑运行会话；重排密钥归属、换 endpoint／跨来源占位拒借，核原 writer 与真实序列化，不以 helper metadata 代替真实入口。

## 5. 工具、Skill 与指令管理切片

- [x] 5.1 接入无Session工具目录／参数说明／策略及两种开关，沿工作区ToolSelectionStore按Agent写覆盖并说明同Agent会话影响；PATCH按tool_id只替换changed实体，不丢其余行或强制再GET全目录。模型测试显式选非空Provider及次数、说明费用；保留后端空数组合同和原run_id进度语义，不混为插件启停／Session配置，默认零审批轮询（A05）。
- [x] 5.2 接入 Skill 来源配置与已有刷新，缺必要操作时仅在所属 Gateway 本机或 Workspace owner 补齐，复用原元数据、资源 URI 及发布；远端 Gateway 投影保持只读，不扩展联邦刷新或新建 Skill 数据库（A06）。
- [x] 5.3 接入Skill来源／错误及正式URI打开：包贡献只读、已有可写资源按原owner；不新增副本流程／owner。Gateway本机刷新与Workspace刷新分开，远端Gateway投影只读、不新增联邦刷新；来源刷新不改插件开关／Thread跟踪或当前封存Turn（A06）。
- [x] 5.4 为 AGENTS、提示词及实际上下文来源提供直达和归属说明，复用文本／Markdown／原检查组件，不恢复 memory（A07、R12）。
- [ ] 5.5 完成V4：秘密目标／保存前取消／响应丢失后只查原键、无泄密；工具覆盖影响同工作区Agent、部分PATCH回执不清空目录；测试非空Provider／次数明确且不自动触发；Skill原URI只读、远端Gateway不刷新、Workspace刷新不改封存版本。工具测试用原可控fixture，不为验收请求付费模型。

## 6. 插件与 MCP 管理接线切片

- [x] 6.1 复用自有包列表／详情、安装／更新／卸载与单插件作用域开关，补实例状态、日志和设置直达；核新建工作区默认不改存量（P01–P04）。
- [x] 6.2 补自有下载源已有配置操作与空源说明，接入原目录查询／安装进度和失败定位；无公网源不发市场请求（P05）。
- [x] 6.3 完善原 MCP 列表／Server Editor 的管理目标、添加、连接／停止／授权／取消及私有参数入口；不显示未实现 resources/templates 为可用（P06–P07）。
- [x] 6.4 以 V5 对插件与 MCP 的新增入口做真实 owner 验收，复用领域已有生命周期证据，不为管理页重写监督或重跑全套长流程。

第6组新增接线使用完整 Workbench／真实 Gateway、Backend 与 MCP fixture 验收：`run-03f2e5eec5d1` 的 workspace／gateway 两范围 2 passed／164.90s，覆盖精确插件版本参数、来源直达、取消零写、两次唯一连接操作和自有下载源原 Settings writer；Gateway 插件参数请求不携带消费工作区，空源零目录／市场请求，resources/templates 明确未提供。安装／更新／卸载、范围默认及实例日志复用原领域已记录证据，未重写监督或秘密 owner；准确收据、依赖复用边界及未验平台见 design.md。

## 7. 会话与运行资源入口接线切片

- [x] 7.1 核 Session／Thread、子线程、运行队列和现有停止操作的直接入口，补有查询合同范围内的运行概览；不新增调度器或跨目标扫描（R01–R02）。
- [x] 7.2 核自动化和会话关联任务共用身份，保留 Goal 开始／编辑／暂停／继续／重新开始／删除、用量和原控制语义（R03–R04）；Goal 各转移未逐一做真实后端生命周期验收。
- [x] 7.3 核文件默认内嵌预览、显式原生编辑器打开、Diff 与审查状态；不改为默认原生打开或新增完整 Git SCM（R05–R06）。
- [x] 7.4 核终端／浏览器／端口原入口及 Thread 关联跳转，复用现有产品与 Gateway 路由，不重写 Terminal／Output 或新增转发实现（R07–R09）。
- [x] 7.5 核 Run and Debug 的已支持插件贡献和 DAP 操作可达，界面说明实际语言／调试器支持，不承诺任意调试器（R10）。
- [x] 7.6 核 Search／Search Editor／Outline 与上下文、Trace、LLM 日志的直接跳转；保持搜索显式范围和日志／历史有界读取（R11–R13）。
- [ ] 7.7 完成 V5 的剩余入口验收；纯接线用代表场景及原领域证据，修改领域行为才扩大相应回归，不以静态注册证明业务可用。

## 8. 诊断、用量和维护切片

缓存复验使用 Quick索引6的 `ui:protected_recovery`：正式 Web `quick_access/test_recent_history/run-133b7b960fa4` 与 Linux development ASAR 验证精确 Thread 清理、其他 Thread 缓存及后端历史／草稿保留。旧 `run-72f8a8b0ff83` 原件无法定位，不作为完成证据。该复验覆盖客户端缓存与精准清理部分；存储位置／保留入口及其余维护项沿 design.md 各自记录。

- [x] 8.1 复用产品日志 owner 与现有诊断组件，提供进程／实例选择、有限尾读、错误定位和安全诊断复制；展示版本／构建／目标（C08、O01、O05）。
- [x] 8.2 为 Gateway 遥测／统计原配置和已有状态补原 Settings 分类及详情入口，不新增 daemon／每请求自检（O02）。
- [x] 8.3 展示原 Thread／Goal 用量与预算；已有 owner 支持的显式范围才聚合，缺价格／覆盖显示未知，不扫全历史或全部 Gateway（O03）。
- [x] 8.4 整理客户端缓存、各存储位置及保留配置入口，接入已有精确清理命令；无 owner 的清理／备份／修复准确说明，不开放目录删除（C07、O04）。
- [x] 8.5 完成 V7：真实 Session Trace HTTP 在十万条合法事件上按原有界页读取；大进程日志只读固定尾部；诊断摘要排除模型正文／API key／token／错误和路径；精确清 B Thread 缓存不删除其他 Thread 缓存、后端历史或 Composer；无隐式扫描／伪造统计。证据与准确收据见 design.md 的 8.5／V7 验收记录。

## 9. 集中验证与减法复审

V6／9.2 收口证据：`run-325671635b33` 的完整真实 Workbench run 验证 Welcome 三入口无 Session／无活动工作区变化，六组 Thread 在 Provider 页面加载／关闭时保留编辑器组，Composer 未发送草稿仍在；关闭 Provider 和插件管理页后，原 Composer 继续提交并收到两条真实 `model_stream` 回复。清理展示缓存后其他 Thread 缓存、后端历史和 Composer 仍可用；管理阶段没有新增审批轮询或全目标插件清点。`out/tests/temp/2026/11/13/gateway_management/artifacts/v6-close-composer-01.json` 记录进程外保护退出码0、85.49秒、build09 main SHA256 `3c41dca6861742b83ca351dbc5edbb7ae57e4877495e40ec1fe8295fbdb07374` 与其构建 SHA 相同；该收据中的测试脚本版本含同一草稿行为，后续只移除了实验性观察者旁路断言。共享 Session Activity 订阅的 owner 是 Workbench `useNavigationRuntime`，管理页面不持有该订阅；本轮以源码归属核对限定其释放合同，没有宣称实际订阅释放事件已由浏览器测得，也不再添加管理页外的后台消息旁路。

- [x] 9.1 每批代码按实际变更做静态分析；前端完成真实类型检查及 `bun run --cwd src/clients/web build`，按注册／构建变化补 Workbench 构建。跨层先核真实序列化与完整消费者，不默认全仓测试。
- [x] 9.2 完成 V6 六组真实 Thread 同屏、管理页面加载／关闭及请求观察，核多 owner 不串用、无新增审批／全目标轮询、无模型／工具自动探测；管理页面只释放本页引用，共享观察者由实际 owner 管理，脏工作副本及已提交业务沿原生命周期，原 Composer 持续可操作。
- [x] 9.3 每个纵向切片仅选择实际新增／改变的最多一个代表列表 API＋一个详情 API各取≥20个暖样本，记录选择依据、一条真实 UI 路径及首开／暖开；按 D8 核 p95／max、request_id、字节和规模，远端功能不硬套回环预算。纯接线复用依赖源码未变的领域证据，不全页面／平台排列，不筛慢样本或无变化重跑。
- [x] 9.4 完成 V8 键盘／命令、Grid、脏草稿／恢复和中英文检查；浏览器侧由上述 `run-325671635b33`、`appearance-product-05` 及 `settings-grid-appearance-05` 覆盖。Linux 已用独立 development ASAR 和正式 `test_native_management_closure` 完成原生启动、同目标管理保存及重启恢复（run-b2933a974c08），准确产物与未验平台见 design；不以 parity 浏览器替代。
- [x] 9.5 全仓检索本次替换入口和消费者后删除旧 demo、死组件、失效测试及兼容开关；从改前代码重推最小实现，删除多余包装、重复状态／校验、轮询、fallback 和持久化机制。
- [ ] 9.6 核对全部矩阵行的实现／受限说明与证据，记录准确命令、源码指纹、原始输出、实际退出码及未验证范围；所有测试使用进程外超时／内存保护，遵守正式／临时产物目录，不抢占现有实例。
