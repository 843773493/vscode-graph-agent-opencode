# 目录用途

验证完整工作台快速访问、原搜索服务与业务 owner 的接线和范围隔离。

# 可修改内容

定向服务集成测试、真实 Gateway／Workbench 浏览器场景及专用装具。

# 不可修改内容

不复制生产搜索、编辑器或 Quick Input 实现，不修改固定上游或运行其测试。

# 规范

服务装具经生产 esbuild 与 legacy decorators 编译真实源码。明确记录 Gateway 传输替身和模型装具边界；真实页面通过 Gateway。产物按测试文件镜像，保留外部超时和内存保护。

`default_contract` 在浏览器中运行真实 Provider，只有 Picker 事件及宿主服务替身，用于调用参数、取消与接受的合同检查；不能将它称为原 Chat／空文件编辑器或完整页面验收。`default_access` 经真实 Gateway 创建关键状态，和原搜索／恢复场景一起使用正式 Workbench 构建。

`test_query_lifecycle` 用隔离 A/B/C、产品 API 创建的 Session 和正式 Workbench 验证搜索分支。名称统一观察 Gateway SSE；`query_gateway_fixture.py` 仅替换本测试进程的出站网络边界，延迟指定真实领域响应或注入具名读取失败，记录 Gateway 实际分发、期限、取消和 request_id。名称及文件读取失败均在同一 Gateway 出站边界注入；身份 401 在明确浏览器 HTTP 边界注入。核对三并发占满时第四分支不分发、超时保留其他结果、单分支重试、停止目标零业务读取及身份失效；不替换匹配、调度、SSE 编码或业务状态，不以这些替身证明真实服务故障。正常名称／文件和正文验收仍使用真实 Gateway／后端。

整份编辑器设置使用 `tests/support/workbench/settings.mjs` 经真实剪贴板粘贴、保存，并只读实际存储核对 JSON；不要用逐键多行插入留下自动补括号生成的无效配置，也不要直接预写 IndexedDB 替产品完成保存。真实搜索观察同样复用该 support 目录；`query_faults.mjs` 只控制本组显式故障装具，不供 E2E 调用。

页面端口复用本目录 `conftest.py` 的租约 fixture，在各测试自己的端口块内有界选择空闲端口并保留绑定，记录探测原件；占用不终止未知进程，其他租约错误立即暴露。

`file_input.test.mjs` 编译真实 `FileEditorInput` 及同一源码转换，只在文件服务／模型引用服务边界提供计数装具；验证并发引用、解析中关闭与失败后重试，不替代真实页面或 Gateway 模型验收。

`change_documents.test.mjs` 使用真实只读 Provider、TextModelResolverService 和 TextResourceEditorModel，仅替换路由／HTTP及底层文本存取；核并发、版本、失败重试和最后引用释放。`test_change_documents.py` 复用 model_stream 通过真实执行创建更改，正式 Gateway 页面从会话资源打开原 Diff，验证分屏、完整补丁、旧引用归一及来源／Diff 刷新恢复，不预写历史或浏览器存储。

`configuration_cache.test.mjs` 使用真实 ConfigurationCache、FolderConfiguration、FileService 和内存 Provider，经唯一文件投影核对非本地配置更新／删除；仅在目录准备边界延迟或拒绝操作，验证单条目顺序、失败传播与独立 key。该检查不替代真实 IndexedDB、页面刷新或打包 Electron；本地设置保存／刷新由 `test_workspace_metadata_bootstrap.py` 的真实入口验证。

`test_configuration_cache.py` 经真实 Gateway 和正式 Workbench 验证原 Profile、A/B 配置归属及正常关闭恢复；故障变体只在独占资产副本的原 IndexedDB Provider 插入精确写入屏障，放行后继续原方法。保留全部资产指纹及唯一插入差异，不修改共享构建、预写 IndexedDB 或变异原型；插桩变体属于 Integration，不代替未修改发行产物或 Electron 验收。

`test_request_content` 经真实消息执行生成请求日志，原页面定位后再核损坏／异查询／catalog变化的续点拒绝；正式Python搜索SDK消费同一Gateway联邦诊断，不在驱动内复制SSE解码。首次选择核活动稳定身份后走真实Enter，后续保留鼠标选择和精确位置断言。

`test_trace_content` 通过执行及 fork 入口创建未打开 child 的真实事件；只选 Trace 后查询并定位原行，原事件面板显示有界上下文与码点高亮。核验同一编辑器接受另一位置、日志追加后的原行读取、旧版本拒绝及零全量 Trace 请求；正式搜索 SDK 继续核对原 owner 的独立续点拒绝。

`test_static_upgrade` 复制正式 Workbench 产物，仅在副本的 main／原 editor Worker 追加可观测版本标记；同一真实 Gateway 和 Chromium 保留 HTTP 缓存，核对内容地址分块热读零正文、main／Worker 再验证304、升级后实际执行新版本及缺失 Worker 失败。副本改写是明确的 Integration 装具边界，不修改共享构建、不预写缓存、不替换响应，不代替打包 Electron 或 Webview 隔离宿主验收。

`test_development_hmr` 使用完整 Git 源码快照和当前差异、固定上游副本及只读依赖链接，经产品 Node／Vite 入口和真实 Gateway 打开页面；只在独占副本中修改导航观测属性，验证自动更新通知及新页面内容，不手动刷新、不替换响应。Vite 可以按原行为自动整页重载；脚本 URL 按路径核对并保留查询参数原件。依赖链接引起的字体访问拒绝及原生关闭诊断如实保留，不据 HMR 通过宣称界面零错误、发行或 Electron 验收。

`test_webview_resource_cache` 经真实 Gateway 和正式 Workbench 打开原 Markdown 预览；CSS、字体及贡献扩展脚本沿原隔离 Worker 加载，无响应替身。核首次正文、热读时仍有 describe 且不重传 raw、更新后新正文与样式、Cache Storage 的 ETag／字体 MIME。模型回放仅提供启动配置，要求零模型请求；本项不代替身份切换、淘汰或打包 Electron 重启验收。回环代理的条件头／304 透传另由 `workbench_server.test.mjs` 的明确 HTTP 替身验证。
