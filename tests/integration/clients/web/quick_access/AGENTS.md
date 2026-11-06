# 目录用途

验证完整工作台快速访问、原搜索服务与业务 owner 的接线和范围隔离。

# 可修改内容

定向服务集成测试、真实 Gateway／Workbench 浏览器场景及专用装具。

# 不可修改内容

不复制生产搜索、编辑器或 Quick Input 实现，不修改固定上游或运行其测试。

# 规范

pytest 启动的 Playwright 页面驱动统一使用 Node，依赖管理与 Workbench 构建继续使用 Bun；Windows 实测 Bun 会在 `chromium.launch()` 挂起，不能以增加超时替代正确运行时。保留原进程外超时、内存保护和业务断言。

`test_resource_opener_sources` 经真实执行生成工具结果，再由正式 Workbench 的原只读编辑器打开、分屏和恢复；复制通过真实命令面板、原 Command 与候选按钮，核对完整 Gateway 引用、原 request_id、零额外正文读取和不打开候选、保留搜索框焦点。剪贴板使用浏览器权限，不预写历史或替换资源响应；不据此证明 Composer 附加、固定版本或 Electron。

`test_composer_resource_actions` 使用真实 Gateway、双工作区、产品创建的 Session 和正式 Workbench；两个可见 Thread 经原输入框写草稿，来源工作区的文件通过原 Opener 打开。引用与选定正文走不同命令及原接收者 Quick Input，核取消不变、更改精确草稿、无额外正文读取、来源修改与刷新后保留正文，以及零执行／模型调用。不预写 IndexedDB 或历史，不以编辑器选项绕过规范 URI 的 Opener，不代替固定版本或 Electron 验收。

发起 Thread 优先场景仅在浏览器 HTTP 边界暂缓真实地址解析回执，期间通过原命令切换活动编辑器组；放行原响应后仍只追加原草稿。该具名延迟不模拟真实 TCP 故障，也不替换来源身份或正文。关闭／切换后的旧句柄和存储失败另由原草稿 hook 的定向测试验证。

同一 Composer 页面从 Thread 的真实命令面板进入原 Quick Access 文件多选，验证勾选后 Esc 不解析引用或改草稿、未打开来源只附加 URI，且无正文读取、无新 Tab、无执行。第二项失败仅在具名 HTTP 边界把该项加入非法 query，交真实 grammar 返回原错误；全部成功场景只延迟第二项真实回执，确认此前草稿未变并切换焦点，最后仍只追加发起草稿。测试仅预备普通文件正文，不预写历史或持久草稿；固定版本和二级返回仍按各自任务验证。

`test_thread_restore_health` 经产品 API 创建 A 的 Session，正式页面打开精确 Thread；B 仅在外部健康 HTTP 边界提供可释放的无响应屏障。核真实 bootstrap 归属／request_id、Composer 可操作总时间及显式健康刷新离线诊断，不预写历史、不替换 A 或 Gateway 响应，不代替真实远端断网验收。

服务装具经生产 esbuild 与 legacy decorators 编译真实源码。明确记录 Gateway 传输替身和模型装具边界；真实页面通过 Gateway。产物按测试文件镜像，保留外部超时和内存保护。

`default_contract` 在浏览器中运行真实 Provider，只有 Picker 事件及宿主服务替身，用于调用参数、取消与接受的合同检查；不能将它称为原 Chat／空文件编辑器或完整页面验收。`default_access` 经真实 Gateway 创建关键状态，和原搜索／恢复场景一起使用正式 Workbench 构建。

`quick_access_controller.fixture` 经同一 sourceTransform 运行真实 QuickAccessController，仅替换 Picker 事件和读取服务边界；覆盖多选确认、勾选后 Esc 取消 reader，以及原单选和 show 的行为，不替代原 QuickPick 的真实 DOM 或 Composer 页面验证。

`composer_selection.fixture` 运行实际资源引用命令与 URI HTTP 客户端，Pane 使用原 ThreadPane 子类，HTTP 回执和 writer 仅作明确服务边界计数；验证逐项解析全部完成后一次追加、第二项失败或取消／不支持时 writer 零调用，以及解析期间切换焦点不改接收者。不把这些服务回执装具当真实 grammar 或产品页面证据。

`test_query_lifecycle` 用隔离 A/B/C、产品 API 创建的 Session 和正式 Workbench 验证搜索分支。名称统一观察 Gateway SSE；`query_gateway_fixture.py` 仅替换本测试进程的出站网络边界，延迟指定真实领域响应或注入具名读取失败，记录 Gateway 实际分发、期限、取消和 request_id。名称及文件读取失败均在同一 Gateway 出站边界注入；身份 401 在明确浏览器 HTTP 边界注入。核对三并发占满时第四分支不分发、超时保留其他结果、单分支重试、停止目标零业务读取及身份失效；不替换匹配、调度、SSE 编码或业务状态，不以这些替身证明真实服务故障。正常名称／文件和正文验收仍使用真实 Gateway／后端。

首范围事件前断流仅将一次真实 Gateway 搜索的浏览器 HTTP 正文截为空，保留原响应头及 request_id；核对失败状态、原请求身份、零自动重试，以及使用同次成功文件流的真实冻结 scope 精确重试一个 Thread 分支，其他失败保留原身份。完全没有 scope 时重查原范围由 Chromium 内容合同装具另核。不将该具名 HTTP 故障注入称为真实 TCP 断连；API 单测另核流错误的机器代码、取消和超时类型。

`test_search_disconnect` 使用独立本地 HTTP 故障服务与生产 `_encode_search_sse`，按真实 Content-Length 截断区分网络错误和正常 EOF；Chromium 原 API 与 Python 搜索 SDK 分别覆盖首范围事件前后，消费确认屏障控制断连。响应头观察器只放行原响应，不替换正文或协议解码；核原 request_id、原网络 cause／HTTPX 类型和零自动重试。该传输检查不替代真实 Gateway 产品页面；Provider 的错误展示及已完成分支保留由 `default_contract` 显式宿主装具另证。

整份编辑器设置使用 `tests/support/workbench/settings.mjs` 经真实剪贴板粘贴、保存，并只读实际存储核对 JSON；不要用逐键多行插入留下自动补括号生成的无效配置，也不要直接预写 IndexedDB 替产品完成保存。真实搜索观察同样复用该 support 目录；`query_faults.mjs` 只控制本组显式故障装具，不供 E2E 调用。

页面端口复用本目录 `conftest.py` 的租约 fixture，在各测试自己的端口块内有界选择空闲端口并保留绑定，记录探测原件；占用不终止未知进程，其他租约错误立即暴露。

`file_input.test.mjs` 编译真实 `FileEditorInput` 及同一源码转换，只在文件服务／模型引用服务边界提供计数装具；验证并发引用、解析中关闭与失败后重试，不替代真实页面或 Gateway 模型验收。

`change_documents.test.mjs` 使用真实只读 Provider、TextModelResolverService 和 TextResourceEditorModel，仅替换路由／HTTP及底层文本存取；核并发、版本、失败重试和最后引用释放。`test_change_documents.py` 复用 model_stream 通过真实执行创建更改，正式 Gateway 页面从会话资源打开原 Diff，验证分屏、完整补丁、旧引用归一及来源／Diff 刷新恢复，不预写历史或浏览器存储。

`configuration_cache.test.mjs` 使用真实 ConfigurationCache、FolderConfiguration、FileService 和内存 Provider，经唯一文件投影核对非本地配置更新／删除；仅在目录准备边界延迟或拒绝操作，验证单条目顺序、失败传播与独立 key。该检查不替代真实 IndexedDB、页面刷新或打包 Electron；本地设置保存／刷新由 `test_workspace_metadata_bootstrap.py` 的真实入口验证。

`test_configuration_cache.py` 经真实 Gateway 和正式 Workbench 验证原 Profile、A/B 配置归属及正常关闭恢复；故障变体只在独占资产副本的原 IndexedDB Provider 插入精确写入屏障，放行后继续原方法。保留全部资产指纹及唯一插入差异，不修改共享构建、预写 IndexedDB 或变异原型；插桩变体属于 Integration，不代替未修改发行产物或 Electron 验收。

`test_request_content` 经真实消息执行生成请求日志，原页面定位后再核损坏／异查询／catalog变化的续点拒绝；正式Python搜索SDK消费同一Gateway联邦诊断，不在驱动内复制SSE解码。首次选择核活动稳定身份后走真实Enter，后续保留鼠标选择和精确位置断言。

`test_trace_content` 通过执行及 fork 入口创建未打开 child 的真实事件；只选 Trace 后查询并定位原行，原事件面板显示有界上下文与码点高亮。核验同一编辑器接受另一位置、日志追加后的原行读取、旧版本拒绝及零全量 Trace 请求；正式搜索 SDK 继续核对原 owner 的独立续点拒绝。

请求日志和 Trace 的删除场景先经产品执行、真实搜索生成候选；`record_deletion.py` 复用原 owner 核验引用及 catalog 路径后，将原件移到本次产物目录，明确注入原位置删除。随后点击仍显示的旧候选，核真实409、原owner／原因／request_id、旧高亮清除及零全量读取／自动重搜；页面完成后移回同一原件，保留下游原目录变更／追加的游标断言。该故障注入不表示产品新增留存或删除入口，不预写历史或替换定位响应。

`test_static_upgrade` 复制正式 Workbench 产物，仅在副本的 main／原 editor Worker 追加可观测版本标记；同一真实 Gateway 和 Chromium 保留 HTTP 缓存，核对内容地址分块热读零正文、main／Worker 再验证304、升级后实际执行新版本及缺失 Worker 失败。副本改写是明确的 Integration 装具边界，不修改共享构建、不预写缓存、不替换响应，不代替打包 Electron 或 Webview 隔离宿主验收。

`test_development_hmr` 使用完整 Git 源码快照和当前差异、固定上游副本及只读依赖链接，经产品 Node／Vite 入口和真实 Gateway 打开页面；只在独占副本中修改导航观测属性，验证自动更新通知及新页面内容，不手动刷新、不替换响应。Vite 可以按原行为自动整页重载；脚本 URL 按路径核对并保留查询参数原件。依赖链接引起的字体访问拒绝及原生关闭诊断如实保留，不据 HMR 通过宣称界面零错误、发行或 Electron 验收。

`test_webview_resource_cache` 经真实 Gateway 和正式 Workbench 打开原 Markdown 预览；CSS、字体及贡献扩展脚本沿原隔离 Worker 加载，无响应替身。核首次正文、热读时仍有 describe 且不重传 raw、更新后新正文与样式、Cache Storage 的 ETag／字体 MIME。Web 使用独立持久 profile，正常关闭 Chromium 后同来源重启，仍通过原 describe 校验而不新增 CSS／字体／保留样式的 raw 请求。业务场景导出后由真实打包 Electron 复用，各宿主显式提供自己的运行上下文；两者的证据分别保留。模型回放仅提供启动配置，要求零模型请求；本项不代替身份切换或其它平台验收，官方 Worker 缓存写入沿原上游尽力而为合同。回环代理的条件头／304 透传另由 `workbench_server.test.mjs` 的明确 HTTP 替身验证。

资源淘汰场景仅通过原 Cache API 删除测试 CSS／字体两条记录，保留其它缓存条目；刷新仍经过原 Worker／宿主和真实文件 owner 重建。该具名故障注入不表示产品新增清理入口，也不证明配额失败诊断、工作台受保护数据清理或 Electron 重启。

`test_language_models.py` 通过真实 Gateway 在 A/B 打开同名 JSON，原 Web 扩展和语言 Worker 计算符号；真实编辑、放弃修改及重开验证分析随模型变化和资源身份隔离。原 token 检查器核对 JSON 语言和属性名 scope，Monaco 数字颜色类核实际渲染；真实语言选择器切换纯文本／JSON后验证旧符号与词法退出、重建及另一工作区保持不变。模型回放只供后端启动且要求零调用，不替换语言服务、预写分析结果或证明迟到 Worker、扩展升级及尚未接入的远端语言宿主。

`workspace_actions.test.mjs` 在 Chromium 中编译真实控制面 Provider、CommandsRegistry 与 HTTP 客户端，仅 Picker／宿主视图 writer 和 HTTP 响应为装具，核 cursor、迟到响应、原查询重试、稳定 owner 与三个独立命令。`test_workspace_actions.py` 经真实 Gateway 和正式 Workbench 验证候选浏览零生命周期写入、范围取消恢复、离线范围选择、明确启动不激活、独立切换及原导航入口；B 只在准备阶段登记并停止，模型回放仅供启动且要求零调用。前者不代替 React writer，后者不证明真实远程 Gateway 连接或多成员 Backend 确认。

`language_analysis.test.mjs` 编译真实 OutlineModelService、LRU、语言 Provider registry 和取消令牌；仅文本模型与异步符号源为受控依赖。核旧版本取消／迟到不删除新缓存、同版本在途共享及模型移除／owner隔离；不将此装具称为真实 Worker，产品语言回归另由 `test_language_models.py` 证明。
