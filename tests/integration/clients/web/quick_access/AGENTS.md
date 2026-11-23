# 目录用途

验证完整工作台快速访问、原搜索服务与业务 owner 的接线和范围隔离。

# 可修改内容

定向服务集成测试、真实 Gateway／Workbench 浏览器场景及专用装具。

# 不可修改内容

不复制生产搜索、编辑器或 Quick Input 实现，不修改固定上游或运行其测试。

# 规范

`file_watch.test.mjs` 从原 Provider 注册入口验证共享 WebSocket 中的文件事件，直接使用原文件事件请求，不保留旧聚合器的伪 subscriptions 或空 connect。原件读取的延迟装具分别阻塞响应头、首块和 EOF，覆盖凭据撤销／Gateway 往返切换时拒绝迟到正文及保存基线、工作区激活变化不改原 owner；不得再通过覆写 `Response.blob` 或 `Blob.arrayBuffer` 模拟已流式消费的路径。

`file_open_menu.test.mjs` 执行正式转换后的真实菜单 Action，真实转换后的原 finder，组服务、编辑器 writer 和打开方式选择结果为明确边界；核发起组作为起点、锁定与复用、后台激活、侧开方向、组关闭拒绝及取消零打开，不将其称作真实 Workbench 页面。`test_file_canonical.py` 的完整页面先单击文件，核会话资源区预览原正文且零新 Workbench Tab，再经原生右键菜单打开编辑器；可复用既有 `file_search_web_assets` 提供的不可变产物，场景 Home／工作区／端口仍独立。

文件搜索合同覆盖显式额外文件的名称／精确正文分支、owner隔离及目录分页前去重；HTTP页资产区分真实文件节点与目录。完整模型覆盖页面通过原文件编辑器修改node_modules中的未保存模型，沿真实Search Editor开启ignore，核本地正文、负命中、原Gateway请求及磁盘不变，不预写模型或替换业务响应。

`test_remote_model_coverage` 使用两个真实 Gateway／Backend 和相同 workspace UUID，经原编辑器修改本地模型，核分区覆盖身份、A+B 小配额、合法续页、仅 A、onlyOpenEditors、首批本地结果及剩余远端配额。SSH 连接沿既有 loopback 装具；仅具名出站失败和一次真实回执延迟为故障边界，原匹配、调度和 SSE 不替换。核真实请求／终态计量、原 owner 身份、模型变化后旧 cursor／重试拒绝，不把状态提示行当正文匹配或将回环验收称为真实跨机。

`scoped_query.test.mjs` 的范围加载场景经真实 sourceTransform、HTTP 客户端和身份事件，在路由或根元数据的明确 HTTP 延迟边界触发凭据失效及 Gateway 往返切换；迟到数据不能形成新范围，活动工作区变化及初次身份发布仍保持发起 owner。完成后验证订阅释放；不将该服务合同称作完整 Workbench 撤权页面。

`test_recent_history` 由真实产品入口创建 A/B 同名 Session，打开 Thread／文件／来源后关闭，并经原 `prepareToClose()` 提交展示态再刷新；只读核原 IndexedDB 的 `history.entries`，不预写存储。冷启动从原范围选择器明确选择 A，不先打开资源补造历史；验证去重、单条移除不关闭 Tab／删除业务、两次恢复及 A/B 草稿。普通页面刷新未完成异步提交、导航默认范围、Electron 和异常退出不由该场景证明。

同页 Thread 恢复负例先经产品打开两组并恢复原草稿，页面卸载后只破坏 B 的一条真实序列化记录，分别注入未知版本、缺失 owner 与坏 JSON；原错误占位页保留具体原因，两次恢复不读取 B，单条移除保留 A、B 业务和草稿。该明确故障注入不代表正常流程预写浏览器状态，也不代替 Electron 固定 profile 验收。

`deleted_thread_history` 复用消息入口真实生成的两份历史，经原 Session Command、确认和目录 outbox 删除已关闭目标，再创建同名 Session；从原最近记录重开必须读取旧 owner 的真实 404、清除其聊天镜像并明确报错，不打开同名目标。仅移除旧最近记录仍保留错误 Tab、幸存 Thread 的正文与草稿及同名业务对象；浏览器存储只读，不预写缓存或替换业务响应。

`test_media_reopen` 通过规范 URI 进入原 Opener／媒体 resolver 与官方隔离 Webview，A/B 同名 WAV 以实际解码时长区分归属；原视频 fixture 核尺寸、时长、复制零新增正文读取、关闭重开和正常刷新恢复。普通媒体输入是文件预备，模型回放只供后端启动且要求零调用；不替换 Worker、媒体内容或业务响应，不把页面通过称为 Electron 或无扩展名附件重开验收。

`test_attachment_media_reopen` 从真实消息上传并执行生成附件，复制原件的无扩展名 `/original/content` URI 后经原 Opener 重开图片、音频和视频；两工作区同名 WAV 用实际解码时长区分 owner。核复制零正文读取、原 HEAD／GET／Range、MIME、名称、request_id、分屏及正常重载；浏览器阶段不执行模型或写文件，不替换媒体与隔离 Worker，不代替 PDF、Windows、远端或 Electron 验收。

`web_extensions_cache_browser.test.mjs` 在真实 Chromium `isWeb=true` 下，经正式 `sourceTransform` 运行原 system／custom builtin scanner、manifest、FileService 与 URI identity，核派生缓存跳过坏项并在坏正文或读取失败时沿原扫描入口回退。缓存内容只作为该读取场景的故障预备，不覆盖用户 profile 扫描或安装行为，也不称作 Gateway 或扩展宿主验收。

pytest 启动的 Playwright 页面驱动统一使用 Node，依赖管理与 Workbench 构建继续使用 Bun；Windows 实测 Bun 会在 `chromium.launch()` 挂起，不能以增加超时替代正确运行时。保留原进程外超时、内存保护和业务断言。

`test_resource_opener_sources` 经真实执行生成工具结果，再由正式 Workbench 的原只读编辑器打开、分屏和恢复；复制通过真实命令面板、原 Command 与候选按钮，核对完整 Gateway 引用、原 request_id、零额外正文读取和不打开候选、保留搜索框焦点。剪贴板使用浏览器权限，不预写历史或替换资源响应；不据此证明 Composer 附加、固定版本或 Electron。

`test_composer_resource_actions` 使用真实 Gateway、双工作区、产品创建的 Session 和正式 Workbench；两个可见 Thread 经原输入框写草稿，来源工作区的文件通过原 Opener 打开。引用与选定正文走不同命令及原接收者 Quick Input，核取消不变、更改精确草稿、无额外正文读取、来源修改与刷新后保留正文，以及零执行／模型调用。不预写 IndexedDB 或历史，不以编辑器选项绕过规范 URI 的 Opener，不代替固定版本或 Electron 验收。

发起 Thread 优先场景仅在浏览器 HTTP 边界暂缓真实地址解析回执，期间通过原命令切换活动编辑器组；放行原响应后仍只追加原草稿。该具名延迟不模拟真实 TCP 故障，也不替换来源身份或正文。关闭／切换后的旧句柄和存储失败另由原草稿 hook 的定向测试验证。

同一 Composer 页面从 Thread 的真实命令面板进入原 Quick Access 文件多选，验证勾选后 Esc 不解析引用或改草稿、未打开来源只附加 URI，且无正文读取、无新 Tab、无执行。第二项失败仅在具名 HTTP 边界把该项加入非法 query，交真实 grammar 返回原错误；全部成功场景只延迟第二项真实回执，确认此前草稿未变并切换焦点，最后仍只追加发起草稿。测试仅预备普通文件正文，不预写历史或持久草稿；固定版本和二级返回仍按各自任务验证。

`test_thread_restore_health` 经产品 API 创建 A 的 Session，正式页面打开精确 Thread；B 仅在外部健康 HTTP 边界提供可释放的无响应屏障。核真实 bootstrap 归属／request_id、Composer 可操作总时间及显式健康刷新离线诊断，不预写历史、不替换 A 或 Gateway 响应，不代替真实远端断网验收。

服务装具经生产 esbuild 与 legacy decorators 编译真实源码。明确记录 Gateway 传输替身和模型装具边界；真实页面通过 Gateway。产物按测试文件镜像，保留外部超时和内存保护。

`session_actions.test.mjs` 运行真实 Session 命令、HTTP 客户端和原 Provider；仅宿主服务、网络响应及导航 writer 边界为装具，复用已有联邦响应资产核只选择与候选按钮，以及分支命令的精确 child／显式展示快照。`test_session_actions.py` 经产品 API 创建 Session，从正式 Workbench 的候选按钮、原导航右键及命令面板执行重命名／删除，沿原 React 对话框、确认和目录 outbox 核真实回执、取消零写入及不打开未选 Thread；模型回放只供启动，要求零模型调用。不预写历史或浏览器业务状态，不代替 Electron 或 Windows 验收。

Thread 分支候选合同同时运行真实名称查询与最近编辑器投影，核同名跨 Gateway 的精确 child、最近项移除／复制／分支按钮索引及零编辑器打开；不以宿主装具代替完整页面的 fork 发布验收。

`default_contract` 在浏览器中运行真实 Provider，只有 Picker 事件及宿主服务替身，用于调用参数、取消与接受的合同检查；不能将它称为原 Chat／空文件编辑器或完整页面验收。`default_access` 经真实 Gateway 创建关键状态，和原搜索／恢复场景一起使用正式 Workbench 构建。

`quick_access_controller.fixture` 经同一 sourceTransform 运行真实 QuickAccessController，仅替换 Picker 事件和读取服务边界；覆盖多选确认、勾选后 Esc 取消 reader，以及原单选和 show 的行为，不替代原 QuickPick 的真实 DOM 或 Composer 页面验证。

`composer_selection.fixture` 运行实际资源引用命令与 URI HTTP 客户端，Pane 使用原 ThreadPane 子类，HTTP 回执和 writer 仅作明确服务边界计数；验证逐项解析全部完成后一次追加、第二项失败或取消／不支持时 writer 零调用，以及解析期间切换焦点不改接收者。不把这些服务回执装具当真实 grammar 或产品页面证据。

`test_query_lifecycle` 用隔离 A/B/C、产品 API 创建的 Session 和正式 Workbench 验证搜索分支。名称统一观察 Gateway SSE；`query_gateway_fixture.py` 仅替换本测试进程的出站网络边界，延迟指定真实领域响应或注入具名读取失败，记录 Gateway 实际分发、期限、取消和 request_id。名称及文件读取失败均在同一 Gateway 出站边界注入；身份 401 在明确浏览器 HTTP 边界注入。核对三并发占满时第四分支不分发、超时保留其他结果、单分支重试、停止目标零业务读取及身份失效；不替换匹配、调度、SSE 编码或业务状态，不以这些替身证明真实服务故障。正常名称／文件和正文验收仍使用真实 Gateway／后端。

`test_text_search_recovery.py` 经真实 Gateway、正式静态产物和浏览器入口验证成功分支保留及失败分支重试，故障只在 Gateway 出站 owner 读取边界注入；对应 Chromium 合同中的 Picker 事件和 HTTP 回执为显式替身，不充当完整产品入口验收。

首范围事件前断流仅将一次真实 Gateway 搜索的浏览器 HTTP 正文截为空，保留原响应头及 request_id；核对失败状态、原请求身份、零自动重试，以及使用同次成功文件流的真实冻结 scope 精确重试一个 Thread 分支，其他失败保留原身份。完全没有 scope 时重查原范围由 Chromium 内容合同装具另核。不将该具名 HTTP 故障注入称为真实 TCP 断连；API 单测另核流错误的机器代码、取消和超时类型。

`test_search_disconnect` 使用独立本地 HTTP 故障服务与生产 `_encode_search_sse`，按真实 Content-Length 截断区分网络错误和正常 EOF；Chromium 原 API 与 Python 搜索 SDK 分别覆盖首范围事件前后，消费确认屏障控制断连。响应头观察器只放行原响应，不替换正文或协议解码；核原 request_id、原网络 cause／HTTPX 类型和零自动重试。该传输检查不替代真实 Gateway 产品页面；Provider 的错误展示及已完成分支保留由 `default_contract` 显式宿主装具另证。

整份编辑器设置使用 `tests/support/workbench/settings.mjs` 经真实剪贴板粘贴、保存，并只读实际存储核对 JSON；不要用逐键多行插入留下自动补括号生成的无效配置，也不要直接预写 IndexedDB 替产品完成保存。真实搜索观察同样复用该 support 目录；`query_faults.mjs` 只控制本组显式故障装具，不供 E2E 调用。

页面端口复用本目录 `conftest.py` 的 fixture，向公共 `TestFilePorts` 申请 Gateway 实际端口并记录；随机选择、占用跳过和绑定持有统一在公共 owner，不再由页面 fixture 扫描候选或重复租约。

`file_input.test.mjs` 编译真实 `FileEditorInput` 及同一源码转换，只在文件服务／模型引用服务边界提供计数装具；验证并发引用、解析中关闭与失败后重试，不替代真实页面或 Gateway 模型验收。

`test_quick_access_interactions.py` 使用独立的正式 Workbench 页面，通过真实 Gateway／Workspace 和 Chromium 验证原 Quick Input 的输入焦点、ARIA、键盘、焦点失去策略、预览恢复和本地设置；服务与 Query Provider 不作替身。运行资产默认沿共享 `web_workbench_assets` fixture；`BOXTEAM_QUICK_ACCESS_WEB_ASSETS` 可显式复用已准备的不可变 dist，收据记录实际路径及 main.js 指纹。独立页面不得改变共享 fixture。

`change_documents.test.mjs` 使用真实只读 Provider、TextModelResolverService 和 TextResourceEditorModel，仅替换路由／HTTP及底层文本存取；核并发、版本、失败重试和最后引用释放。`test_change_documents.py` 复用 model_stream 通过真实执行创建更改，正式 Gateway 页面从会话资源打开原 Diff，验证分屏、完整补丁、旧引用归一及来源／Diff 刷新恢复，不预写历史或浏览器存储。

同一更改页面先从 `resource ` 查找发起 Session 的真实 changeset 摘要，核逐键与取消零预览／审阅写入，再接受指定范围并沿原更改面板打开 Diff；原模型、引用和编辑器导航断言继续保留。

`configuration_cache.test.mjs` 使用真实 ConfigurationCache、FolderConfiguration、FileService 和内存 Provider，经唯一文件投影核对非本地配置更新／删除；在目录准备边界延迟或拒绝操作，验证单条目顺序、失败传播与独立 key。默认配置／schema 更新使用原 Registry／DefaultConfiguration，事件到 reparse 为具名编排回调，核反复重解析零新增读取／缓存写入及实际信任过滤；不代表完整 WorkspaceService 编排。该检查不替代真实 IndexedDB、页面刷新或打包 Electron；本地设置保存／刷新由 `test_workspace_metadata_bootstrap.py` 的真实入口验证。

`test_configuration_cache.py` 经真实 Gateway 和正式 Workbench 验证原 Profile、A/B 配置归属及正常关闭恢复；故障变体只在独占资产副本的原 IndexedDB Provider 插入精确写入屏障，放行后继续原方法。保留全部资产指纹及唯一插入差异，不修改共享构建、预写 IndexedDB 或变异原型；插桩变体属于 Integration，不代替未修改发行产物或 Electron 验收。

`test_request_content` 经真实消息执行生成请求日志，原页面定位后再核损坏／异查询／catalog变化的续点拒绝；正式Python搜索SDK消费同一Gateway联邦诊断，不在驱动内复制SSE解码。首次选择核活动稳定身份后走真实Enter，后续保留鼠标选择和精确位置断言。

`test_trace_content` 通过执行及 fork 入口创建未打开 child 的真实事件；只选 Trace 后查询并定位原记录，原事件面板显示有界上下文与码点高亮。核验同一编辑器接受另一位置、SQLite 提交追加后的原记录读取、旧版本拒绝及零全量 Trace 请求；正式搜索 SDK 继续核对原 owner 的独立续点拒绝。正式静态产物复用原 `chat_loop_web_assets` 的显式准备入口，隔离 Home、端口和运行实例不共享。

请求日志和 Trace 的删除场景先经产品执行、真实搜索生成候选；`record_deletion.py` 复用原 owner 核验引用及 catalog 路径后，将原件移到本次产物目录，明确注入原位置删除。随后点击仍显示的旧候选，核真实409、原owner／原因／request_id、旧高亮清除及零全量读取／自动重搜；页面完成后移回同一原件，保留下游原目录变更／追加的游标断言。该故障注入不表示产品新增留存或删除入口，不预写历史或替换定位响应。

`test_static_upgrade` 复制正式 Workbench 产物，仅在副本的 main／原 editor Worker 追加可观测版本标记；同一真实 Gateway 和 Chromium 保留 HTTP 缓存，核对内容地址分块热读零正文、main／Worker 再验证304、升级后实际执行新版本及缺失 Worker 失败。副本改写是明确的 Integration 装具边界，不修改共享构建、不预写缓存、不替换响应，不代替打包 Electron 或 Webview 隔离宿主验收。

同模块的本地化升级场景从完整源码快照分别正式构建 A/B，只在独占源码中改变产品文案和上游 NLS 补译；Gateway 同端口正常重启、Chromium 同 profile 正常关闭重开。核原初始化 owner／request_id、旧 ETag 的真实条件请求、同批 manifest／语言资源及两份实际界面文案、热读零正文；不手改产物、预写 HTTP 缓存或替换业务响应，也不把源码构建验证称作正式 Electron 发行验收。`source_snapshot.py` 与真实 Vite 场景复用完整 archive／差异／未跟踪文件及固定上游，只链接只读依赖。

`test_development_hmr` 使用完整 Git 源码快照和当前差异、固定上游副本及只读依赖链接，经产品 Node／Vite 入口和真实 Gateway 打开页面；只在独占副本中修改导航观测属性，验证自动更新通知及新页面内容，不手动刷新、不替换响应。Vite 可以按原行为自动整页重载；脚本 URL 按路径核对并保留查询参数原件。依赖链接引起的字体访问拒绝及原生关闭诊断如实保留，不据 HMR 通过宣称界面零错误、发行或 Electron 验收。

`test_webview_resource_cache` 经真实 Gateway 和正式 Workbench 打开原 Markdown 预览；CSS、字体及贡献扩展脚本沿原隔离 Worker 加载，无响应替身。核首次正文、热读时仍有 describe 且不重传 raw、更新后新正文与样式、Cache Storage 的 ETag／字体 MIME。Web 使用独立持久 profile，正常关闭 Chromium 后同来源重启，仍通过原 describe 校验而不新增 CSS／字体／保留样式的 raw 请求。业务场景导出后由真实打包 Electron 复用，各宿主显式提供自己的运行上下文；两者的证据分别保留。模型回放仅提供启动配置，要求零模型请求；本项不代替身份切换或其它平台验收，官方 Worker 缓存写入沿原上游尽力而为合同。回环代理的条件头／304 透传另由 `workbench_server.test.mjs` 的明确 HTTP 替身验证。

资源淘汰场景仅通过原 Cache API 删除测试 CSS／字体两条记录，保留其它缓存条目；刷新仍经过原 Worker／宿主和真实文件 owner 重建。该具名故障注入不表示产品新增清理入口，也不证明配额失败诊断、工作台受保护数据清理或 Electron 重启。

同场景在真实缓存建立后删除生成的 CSS，核 owner 的原404／错误码／request_id、Worker 404及旧样式不再生效；恢复该文件后必须取得新字节和validator。原件操作仅限本测试工作区，不替换响应或预写缓存。`BOXTEAM_WEBVIEW_CACHE_ASSETS` 可显式复用已构建的不可变静态产物，保留主包指纹；未指定时仍沿公共构建fixture。

非 root POSIX 同页对已有缓存的 CSS 原件撤销读取权限，要求原 describe 返回 PermissionError／request_id，隔离 Worker 拒绝旧字节且其它样式保持；恢复权限后同一缓存经当前描述复用，零额外正文读取。保留故障时 Cache Storage 原件，不清浏览器数据；Windows ACL 仍需目标机器的独立验收。

`test_language_models.py` 通过真实 Gateway 在 A/B 打开同名 JSON，原 Web 扩展和语言 Worker 计算符号；真实编辑、放弃修改及重开验证分析随模型变化和资源身份隔离。原 token 检查器核对 JSON 语言和属性名 scope，Monaco 数字颜色类核实际渲染；真实语言选择器切换纯文本／JSON后验证旧符号与词法退出、重建及另一工作区保持不变。模型回放只供后端启动且要求零调用，不替换语言服务、预写分析结果或证明迟到 Worker、扩展升级及尚未接入的远端语言宿主。

`workspace_actions.test.mjs` 在 Chromium 中编译真实控制面 Provider、CommandsRegistry 与 HTTP 客户端，仅 Picker／宿主视图 writer 和 HTTP 响应为装具，核 cursor、迟到响应、原查询重试、稳定 owner 与三个独立命令。`test_workspace_actions.py` 经真实 Gateway 和正式 Workbench 验证候选浏览零生命周期写入、范围取消恢复、离线范围选择、明确启动不激活、独立切换及原导航入口；B 只在准备阶段登记并停止，模型回放仅供启动且要求零调用。前者不代替 React writer，后者不证明真实远程 Gateway 连接或多成员 Backend 确认。

`language_analysis.test.mjs` 编译真实 OutlineModelService、LRU、语言 Provider registry 和取消令牌；仅文本模型与异步符号源为受控依赖。核旧版本取消／迟到不删除新缓存、同版本在途共享及模型移除／owner隔离；不将此装具称为真实 Worker，产品语言回归另由 `test_language_models.py` 证明。

`target_settings.test.mjs` 编译真实默认 Agent 来源 Command 与来源 API；仅 Quick Input、通知、命令交接和 HTTP 回执是服务边界，覆盖 owner／来源取消、进入 Agent 表单的精确 owner 与只读状态/pending 操作。`test_target_settings.py` 经正式 Workbench、真实 Gateway 与 A/B 工作区，从原命令面板选择来源进入 Agent 表单，通过同一 JSONC 模型设置 `default_agent` 并经来源保存；核保存确认取消、导航切换后的冻结目标、未知回执只查原幂等键、真实 source_saved／layer_imported／pending_restart 提示、原 pending discard／active CAS 及只读来源。模型回放仅供启动且要求零调用；不预写配置文件或浏览器存储，不代替 Backend 重启健康证明或原生客户端验收。

只读恢复故障验收先经产品生成更改、来源及正常编辑器组状态；离开产品文档后，只改坏该组中一条来源序列化记录，分别注入未知版本和损坏 JSON。核重复刷新保留错误、零来源读取、相邻 Diff 正常恢复，以及原占位按钮只移除展示记录；新打开继续通过 Quick Open 的规范 URI Opener。该明确存储故障注入不代表正常业务预写状态，不代替 Electron 或所有资源删除场景。

`test_localization` 经过真实 Gateway／Workspace 与正式 Workbench，英文浏览器在无偏好时默认中文，语言只经原 Quick Pick／清除命令改变；多 Thread 草稿、脏文件及分屏从产品入口生成，核取消重载、双向重载恢复、插件管理双语和晚创建 Markdown 的本地翻译。第三个 Thread 经 Composer 接受实际执行，复用 model_stream 的精确调用身份与响应屏障，核两次语言重载期间同一 Job 保持运行、一次模型调用及释放后的原文回复；只在本组 fixture 选择既有受控 ASGI 装配，不改变通用 metadata fixture。`BOXTEAM_LOCALIZATION_WEB_ASSETS` 可显式复用本批已成功构建的不可变产物，收据保留路径、版本及主包指纹；未设置时沿公共构建 fixture。端口、Home、工作区及进程监督仍沿本测试 owner，不使用开发实例，不以页面通过代替 Electron 验收。

`localization_scenario.mjs` 只共享这组语言业务步骤；Web 与真实 Electron 分别创建自己的运行 owner、工作区、端口及客户端。原生入口额外核同窗口重载和正常关窗恢复，不由 Web 页面结果代替。

`thread_cache_cleanup.mjs` 共享明确 Thread 镜像清理与受保护恢复业务步骤，产品执行状态由 `cache_cleanup_case` 通过真实消息入口生成。Web 沿原展示提交与刷新，Electron 沿正常关窗与同 profile 重启；两者分别核文件历史、dirty 备份、分屏视图、草稿、设置及其它 Thread 保留，不预写历史或恢复状态。

同一语言场景只拒绝下一次真实展示 IndexedDB 写入，核具体原因和零重载，随后恢复原写入。脏文件取消经用户设置入口关闭 hotExit，再拒绝原 beforeunload，不能把 Application scope 设置写入工作区来假定生效；图片和 Markdown 均经原 Opener 与原预览控件验证双语。不新增生产测试 hook，不预写恢复状态。

`bootstrap_localization.test.mjs` 使用实际 bootstrap、浏览器动态模块及当前完整词典，只有 main 求值观察器、具名资源503与Storage读取失败是显式替身。`locale_service.test.mjs` 经正式源码转换运行实际语言服务及宿主方法，在对话框、Storage写入与展示提交依赖注入取消／失败，核原错传播及零shutdown；两者不替代真实Gateway页面、原生Electron或完整发行验收。

`test_workspace_actions` 的条件模式场景通过真实 Gateway 和正式 Workbench，遍历原帮助虚拟列表，核 term／task／debug／debug consoles 的真实空态、目标插件库存回执及安装来源选择后取消。mcpr／issue 按实际注册条件记录缺失，不把空列表或源码同源当作运行互通及 Electron 验收；POST 仅放行具名只读查询和订阅。可用 `BOXTEAM_WORKSPACE_ACTIONS_WEB_ASSETS` 显式复用本批不可变资产，保留主包指纹；Home、端口和进程仍归本用例隔离。

`recent_history.mjs` 与真实 Electron 共用产品创建、原最近资源／命令历史及三项 preserveInput 配置的业务步骤；先关闭原搜索动作的选区种入以独立验证输入记忆。坏 JSON／未知版本／缺失 owner 只在离开产品文档后破坏一条真实 Thread 记录，重启保留原错并单条移除；正常状态不预写历史。凭据负例只改下一次真实 HTTP 发送边界的 token，401／原因／request_id 均由 Gateway 产生并原样返回。工作区通过原 managed API 删除登记、在隔离测试工作区搬迁及复用原路径，核旧稳定身份拒绝替代目标、重登记原 UUID 后恢复草稿；原 HistoryService 的打开失败单条移除行为保留，恢复从原规范 URI 进入。不新增产品 hook／IPC，不代替真实双 Gateway 切换或其它平台验收。

`item_documents.test.mjs` 通过原 FileService 及只读 Item Provider 验证冻结引用、stat 零预读、运行中取消到 HTTP signal、字节范围和失败重读；目标解析、HTTP 及编辑器关闭事件为受控依赖。关闭最后一组取消原读取，另一组仍打开时保留；Gateway 离开后切回仍拒绝迟到路由。已取消令牌的零请求检查直接经过 Provider；上游 FileService 对预取消父令牌的传播可能延后，不把此项当作整个编辑器关闭的页面验收，真实页面关闭验收归原 context history markers 场景。

默认入口合同装具模拟原 Controller 隐藏时同步释放 Provider lifetime，并在无 selectedItems 时取消令牌；符号行按钮通过原 Picker 事件经过异步 resolve，分别核打开后关闭和 Esc 取消。真实 Trace 场景使用原 navigateBack／navigateForward 在同 Thread 的两处精确命中间导航，核实际 locate 请求和可见原文，不预写历史栈。

发起组合同覆盖空组及模式转接，符号 Provider 的当前活动组与显式发起组必须不同。真实 Trace 场景关闭定位后通过原分屏命令复制 Pane 视图态，核无新增 locate 请求且不恢复已关闭的定位。

大纲加载取消合同实例化真实 ChatSurface，只用子类替换渲染出口及具名阅读控制器，核临时卸下／重新挂载、立即恢复展示、恢复位置仅执行一次，以及后续接受替换旧恢复；不将宿主边界检查称作真实 Gateway 页面验收。

Thread 视图态合同运行真实 Pane setInput／clearInput、ChatSurface 和原 memento；仅路由解析、工作台服务和内存 Storage 为具名依赖边界。核标签切回、关闭重开、组隔离、原 restoreViewState 开关、明确定位优先及恢复事件原因；不代替页面重载和真实 Gateway 验收。

阅读恢复合同分别核控制器已挂载和延迟挂载：PROGRAMMATIC 只恢复位置且不切换展示，NAVIGATION 才进入聊天并采用主动定位；通过真实 ChatSurface 的统一入口验证，不以具名控制器证明实际日志页或浏览器重载验收。
