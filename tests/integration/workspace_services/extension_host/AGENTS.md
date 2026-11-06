# 目录用途

验证固定上游 Node Extension Host 的构建、Backend 进程基础设施、原 IPC／RPC、真实扩展激活及目标文件访问。

# 可修改内容

- 明确主线程替身边界的宿主协议测试、进程生命周期测试和测试扩展。

# 不可修改内容

- 不实现产品宿主管理、安装账本或另一套扩展协议；不运行上游测试。

# 规范

- 使用显式 Node runtime 和隔离依赖，产物按测试路径写入 out/tests；所有运行带进程外超时和内存保护。
- `test_windows_pipe.py`直接核Windows原管道的当前用户唯一受保护ACL、单实例、双向大正文及首次调度前／后的取消回收；不能用构造参数或替身句柄代替这些原生断言。
- 原型不代表 Gateway、目标授权、多目标窗口 registry 或完整产品验收。
- `test_node_host.py`使用真实Backend registry与原BackendLifespan关闭宿主；ipc直接接owner，gateway/relay使用真实Uvicorn与产品认证路由，TCP listener只桥接原协议字节。Uvicorn关闭完整lifespan装配，未装配工作区业务图和真实SSH；窗口主线程仍是后续验收边界。
- `extension_state.test.mjs` 编译真实描述类、接收端、Registry、Sequencer、原 Manager 和生命周期 owner；只在文件 realpath、进程／RPC 传输和主线程服务边界提供明确装具。候选发布、失败代际移除和 eager 并发必须经过这些原方法，不能复制状态机；该组合验证不等同 Gateway 授权或完整多目标产品验收。
- 内部回调代际验证使用原owner生成的API及原eager／lazy Manager关闭入口；覆盖断连等待／失败、直接dispose、迟到激活和同位置替换，不能用测试自行设置active变量代替产品准入。
- 查询／激活验证运行原MainThread、owner、Manager和ExtHost activator；仅窗口TimerService的DI模块为具名替身，不能据此宣称验证窗口性能服务或完整视图投影。保留`api:none`依赖成功／失败与原错误归属，核非所属状态回调拒绝、唯一持有者派发和歧义零调用；不能只检查改写后的源码文本。
- `node_host.mjs` 通过真实原 RPC 增删、替换同 ID 的未激活扩展，并在合法版本激活真实代码；回调观察真实 `extensions.onDidChange`，核旧／重复版本与非法候选拒绝及另一宿主不变，不据描述替换宣称支持已激活扩展动态卸载。
- `test_extension_management.py`经Backend原owner启动真实Node组件，再由原生安装器生成profile；覆盖双Backend同ID不同版本、重启恢复、重叠管理锁、安装失败及损坏记录。故障注入只能在真实安装后修改原件；缺失manifest沿原scanner报告invalid和原因，不能弱化为空列表，也不能把基础设施验证表述为窗口安装UI／实例registry已完成。
- 本目录真实HTTP统一由`conftest.py`持有已绑定socket与Uvicorn任务并等待关闭；库存网络场景经过原Gateway认证、Backend私有入口及真实Node/profile，核双Backend版本、稳定身份、恢复和route误指，不装配完整工作区业务图，不代表窗口registry、真实SSH或VSIX安装界面验收。

- `extension_storage.test.mjs` 在独立本机HTTP来源的真实Chromium中运行原BrowserStorageService／IndexedDB／BroadcastChannel、ExtensionStorageService、MainThreadStorage、ExtHostStorage及Memento；host binding／RPC为具名装具，不能当Gateway／真实Node验收。核同ID目标及稳定视图隔离、Web原键／同步清理、旧代际拒绝、跨窗口通知、整值并发和新窗口恢复；不宣称跨设备同步。`node_host.mjs`另由真实扩展原公共API实写三个目标存储file URI、核包目录分离及setKeysForSync同步明确拒绝，主线程Memento仍是该原生协议装具的具名替身。
- 同一存储回归运行原 MainThreadSecretState／ExtHostSecretState，并编译 Node 专用入口，核四类秘密操作在 RPC 前拒绝、主线程零秘密读写／广播及 Web 原 API；秘密服务为具名装具。原生 probe 另经真实 Node 的公开 SecretStorage 验证四类拒绝，不能据此宣称目标侧秘密 owner 已实现。

- `extension_diagnostics.test.mjs` 运行原 MainThreadDiagnostics／MarkerService／ExtHostDiagnostics／DiagnosticCollection 及 RPCProtocol，验证同名集合、重叠视图、代际清理、整批准入、事件与关联位置投影、目标原生帮助链接及 Web 原合同。认证 binding、字节传输及文档版本服务为具名装具，不代替 Gateway、真实 Node 进程或浏览器页面验收；沿 `integration-extension-host-state` 的外部超时与内存保护运行。

- `extension_language_features.test.mjs` 在真实 Chromium 中运行原 MainThreadLanguageFeatures、共享 LanguageFeatureRegistry、wordHighlighter 多文档入口及 RPCProtocol／TargetUriTransformer；仅认证 binding、内存字节输送、扩展回调和模型的 URI／语言／同步状态为具名装具。覆盖全部文档 selector 注册、目标原生 glob／RelativePattern、exclusive／候选缓存、secondary 归属及真实 RPC 回调／取消／关闭；Windows 仅评分逻辑，不能替代 Gateway、真实 Node、文档同步、Notebook cell 或 Windows 实机验收。

- `extension_workspace_symbols.test.mjs` 运行原 MainThread、WorkspaceSymbolProviderRegistry／getWorkspaceSymbols、RPC、AST 提取的内置命令和真实 CommandService／DI；认证 binding、字节输送、扩展回调及扩展服务清单／激活等待为具名装具。核完整 view 准入、typed URI／resolve／cache、取消／代际及命令来源，不代替 Gateway／Node／SSH 或符号界面验收。

- `extension_workspace_edits.test.mjs` 在真实 Chromium 中经原公开 API、ExtHostBulkEdits、RPC、MainThreadBulkEdits 和 BulkEditService 核整批归属、代际及现有版本字段在首项副作用前的预检；文本用原 TextModel，文件用原 BulkFileEdits／WorkingCopyFileService／FileService 与内存 Provider。覆盖预览最终集合、多资源解析等待中的版本变化、重复资源引用、解析失败／取消回收及无版本语义；Notebook 仅核已有版本 DTO／ResourceEdit 和具名 resolver 的冲突零写入，opaque 回调直接沿原 BulkEdit／OpaqueEdits。认证 binding、字节输送、文档版本镜像、文本 resolver 的延迟／失败／按需创建原模型、配置／UI 和纯文本 TreeSitter 库为具名装具；引用只观察请求／释放，不证明原 resolver 的引用计数及关模／重开保护。不替代真实 Gateway／Node／磁盘保存、产品 WorkspaceFiles 的重命名／删除、扩展 FileEvents participant、Notebook 真实编辑或该用例未接入的 CodeAction／Notebook 版本 producer。

- `extension_code_actions.test.mjs` 在真实 Chromium 中运行原 MainThreadLanguageFeatures、LanguageFeaturesService 注册表、RPC、CodeActionItem 和 applyCodeAction；版本 producer 另经原 ExtHostLanguageFeatures／CodeActionAdapter、WorkspaceEdit.from、CommandsConverter 和 ExtHostDocuments／AndEditors／DocumentData。核 provide／resolve 调用前版本冻结、等待及发布后变更、同原生 URI 的 A/B 隔离、create／未同步资源不造版本；文档 delta／change 输入、扩展 callback、空诊断与命令注册接收端为具名边界。错误呈现场景借用原 CodeActionController.applyCodeAction／_trigger，核心仍经原 DI；editor、进度、模型触发和通知接收端为具名 UI 装具，核同一错误／cause、core 拒绝、等待顺序和零后继命令。原 DTO 准入／生命周期场景的认证 binding、字节输送、扩展 callback／cache、模型 URI／语言仍为具名装具；保留整批归属、原 action 身份、应用取消、注销／宿主关闭、等待、新代际隔离、准确 cache 释放与 Web／内置直接 action。BulkEdit、命令与 UI DI 服务为边界装具，内存效果不是真实磁盘写入；不替代真实 Node／Gateway、BulkEditService、命令处理器、UI 点击、Notebook 版本 producer 或其余 Provider 验收。
