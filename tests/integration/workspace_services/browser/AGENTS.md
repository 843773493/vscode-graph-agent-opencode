# 目录用途

存放 Browser Manager 使用测试页面服务、受控网络条件或其他替身边界的集成测试。

`test_browser_creation_intent.py`的创建取消场景经真实Node／HTTP／Chromium，分别暂扣context交付和已完成页面注册的原回执，核管理版本、原创建与取消共同结算、磁盘failed/verified、真实context只关闭一次及peer继续执行。原close/delete拒绝与SIGKILL后无身份unknown场景保持；临时preload仅观察和暂停原回执，不改写业务事实。定向pytest须用进程外超时／4GiB和原集成闸门；不代Python lease、Backend／Gateway／Web重启或Windows验收。

同文件另以正式Session/资源创建生成Python记录与原dispatching lease，真实强杀Python，再由独立冷owner恢复并管理stop仍在原Node执行的创建。分别暂停context交付/页面注册，核领域creating、原身份与精确lease、取消前未结清、原取消及停止顺序、真实context只关闭一次与最终冷账本settled；不代完整Backend服务进程、Gateway/Web、Node硬重启或Windows。

```text
browser/
├── test_browser_custom_tools.py        代理工具与Browser Manager接线
├── test_browser_resource_performance.py 资源治理、帧传输与三平台性能采样
├── browser_element_references.mjs       真实DOM中的元素引用与动作
└── browser_checkpoint_form_state.mjs    表单检查点捕获、释放与恢复
```

`browser_element_references.mjs` 直接使用真实Chromium验证页面摘要、坐标检查与按ref点击的组合，覆盖动态新增、移除、重排、文档版本变化，以及摘要达到元素上限后停止尾部可见性检查；通过 `bun run test:matrix -- --suite=integration-browser-element-references` 运行，固定60秒/4GiB。它不启动Gateway或主Web，也不属于服务端纯单元测试矩阵；Chromium临时文件写入本测试正式输出的runtime目录。

坐标检查同时核CSSOM在一次检查内只整理一次、嵌套与继承规则仍可读、下一次能看到规则更新，以及跨域规则不可读时仍返回可读来源与最终样式；计数断言限定重复解析成本，不以机器耗时作为通过条件。

摘要标签场景使用真实DOM及保留原生行为的getter计数：验证aria-label、alt、title、表单值与正文的优先级、空白高优先级标签、文本回退及UTF-16截断；已有标签或值时不预读完整正文。计数只覆盖元素标签读取，整页body文本仍正常提取，不能据此宣称全页摘要成本恒定或整体提速。

`browser_checkpoint_form_state.mjs` 验证混合控件的精确定位、值/选择/存储/滚动位置恢复，DOM变更后重新采集，以及大型匿名表单的有界兄弟扫描；使用真实BrowserSession、运行时池、Chromium和受控HTTP页面，Manager持久化为空操作。通过 `bun run test:matrix -- --suite=integration-browser-checkpoint-form-state` 运行，固定60秒/4GiB，原生临时目录归本测试正式runtime；不代表磁盘检查点或Gateway/主Web验收。

`browser_shutdown_operations.mjs` 经真实Manager创建、冻结/冷回收和交互入口，使用HTTP响应与动作屏障覆盖准备、执行、Session排空超时及Manager默认预算超时后的保留/重试，并核对双Browser在首项等待时全部关闭准入、整批共用一次默认5秒预算、原操作完成后两份检查点冷恢复；核对关闭顺序、原页面终态、磁盘检查点与冷Manager重开后的表单值。原生检查点目录冲突同时触发采集/收尾失败，核对两份首因、其它Browser完成检查点、失败资源unknown及原身份重试与健康资源冷恢复。通过 `bun run test:matrix -- --suite=integration-browser-shutdown-operations` 运行，固定60秒/4GiB；不代表其它失败收尾、挂起动作终止、Gateway或主Web验收。

同一关闭owner另覆盖首次导航仍在真实HTTP等待时的创建：关闭等待原创建、默认5秒创建超时保留原页面/上下文、同Manager重试关闭与冷恢复，并在已有peer操作时证明创建等待与交互共用总预算，健康Browser可先完成检查点。关闭后新创建必须携原intent的not_created回执拒绝，不能产生新记录。失败断言只输出标量观察，避免格式化完整Playwright对象图遮蔽原错误。

`test_browser_owner_revision_drain.py` 经Python客户端、真实Node与Chromium验证默认5秒的旧版本排空超时、持久新版本、原动作仍执行、独立Browser可读、迟到回执拒绝和同版本重试；受控HTTP服务只暂停原脚本的fetch。通过 `bun run test:matrix -- --suite=integration-browser-owner-revision-drain` 运行，固定90秒/4GiB；不代表Python业务owner的恢复gate隔离、强制终止或Gateway/主Web验收。

`test_page_registration_close.py`复用原Browser装具，按当前文件隔离工作区、端口和产物；通过正式创建和run入口生成真实Chromium弹出页，仅暂停原Network.enable回执交付。原关闭须先408并持久保留unknown，独立页面仍可操作；释放原回执后显式重试才核实终态，页面不得复活。定向pytest沿原独占闸门、进程外超时和4GiB保护；不代替Python业务owner、Gateway/主Web、硬重启或Windows验收。

`test_download_close.py`复用同一装具的文件隔离；正式run触发真实Chromium下载，原StateStore保存字节后仅暂扣回执。核关闭408、磁盘unknown、下载正文仍在且记录尚未发布、peer继续操作，以及放行原回执后同资源关闭才verified并发布原下载身份。定向pytest沿原独占闸门、外部超时和4GiB保护；不代表下载网络永久挂起、强制终止、Python业务owner、Gateway/Web或Windows。

`test_page_close_drain.py`复用原装具并按当前文件隔离；正式创建与新增标签页生成两个真实页面，仅在原指针reset完成后暂停回执。由peer关闭原活动页面，核原生关闭已发生、资源关闭408及磁盘unknown；peer仍能操作，解除原回执后同资源重试才verified，并已保存原关闭事件的迟到激活错误。定向pytest沿原独占闸门、外部超时及4GiB保护；不代替Python业务owner、Gateway/Web、硬重启、Windows或完整shutdown验收。

`test_browser_operation_receipts.py`通过正式Session、ASGI资源创建/管理与原工具operation运行真实Chromium脚本，先产生页面副作用再失败。仅在真实失败回执版本或首次记录提交处注入故障，核对原lease冷读仍待核实、原页面及版本保持、普通查询不结清，显式管理停止核实后才settle；完整失败仍保留原HTTP错误及正常释放语义。suite `integration-browser-operation-receipts`固定90秒/4GiB，不代表Gateway/主Web或独立Backend重启。

同一文件的stop/delete场景让脚本忽略取消signal，在工具超时后继续原fetch；核对页面reset可用、关闭context后仍返回408且磁盘unknown、peer可读，以及放行原执行后同身份/版本重试终态。原页面只关闭一次，不将关闭context或工具超时当作脚本已结束；这仍不是Python业务owner挂起操作接管或强制终止验收。

同一文件经真实Session/Python业务owner创建资源、正式detach及run入口，令超时后的原脚本等待context关闭，再由原管理stop/delete完成撤权、释放context和终态核实；核对当前版本、原lease结清与peer可读，不手填资源或执行状态。它不代表任意脚本强制终止、Backend重启或Gateway/主Web验收。

`test_browser_shutdown_signals.py` 通过真实Node/Chromium/HTTP、受控网络屏障和POSIX的SIGTERM/SIGINT/SIGHUP验证：原5秒排空失败后进程、原页面和管理查询仍可用，准入继续关闭，在途重复信号不重发关闭；原操作完成后再次信号保存最终检查点并退出，新进程以原ID冷恢复最终表单值。另核未捕获异常与未处理rejection遇到同类排空失败时仍按原规则退出1，以及关闭过程中到达的致命错误不会被在途去重忽略。通过 `bun run test:matrix -- --suite=integration-browser-shutdown-signals` 运行，固定60秒/4GiB，各场景日志和结果独立保存；不代表Windows SIGBREAK、上层启动器、Gateway/主Web、强制终止或整个shutdown总期限验收。

`test_browser_resource_listing.py` 复用原辅助进程fixture并按当前测试文件隔离产物，经正式Session/资源创建、真实Node/Chromium/HTTP验证清单等待期间peer查询及工具完成；正式stop或同版本外部关闭观察提交后，旧回包必须冲突，原owner/lease字节不回退，刷新只取一次清单。通过 `bun run test:matrix -- --suite=integration-browser-resource-listing` 运行，固定120秒/4GiB；不代表Gateway/主Web、Windows、网络永久挂起或完整shutdown验收。

`test_browser_governor_diagnostics.py`只替换Linux内核文件读取，经过原采样器、调度器与真实Node/HTTP健康接口验证cgroup目录不公开、采样失败停止并保留上次事实、完整原错留诊断，以及原服务重启后重新采样。suite `integration-browser-governor-diagnostics`固定60秒/4GiB；不创建Chromium，不代表Gateway/主Web或其他平台实机验收。

`test_browser_websocket_input_errors.py`经真实Node WebSocket/HTTP验证非法JSON及九类未知字段的POSIX、盘符和UNC输入只进入诊断、零Browser/context副作用；同连接继续以真实授权附加/分离正式HTTP创建的本地文件页面。suite `integration-browser-websocket-input-errors`固定90秒/4GiB；不代表Gateway/主Web、Python业务owner、进程重启或其他WebSocket运行时失败验收。

`test_browser_runtime_diagnostics.py`与`browser_runtime_diagnostic_failure.mjs`仅在原Chromium启动参数及日志writer边界注入故障，通过真实Node/HTTP核对原生启动失败、日志目录访问失败、原创建回执、只读磁盘记录及故障解除后的真实页面；观察writer尝试不充当产品日志回退。suite `integration-browser-runtime-diagnostics`固定60秒/4GiB，不代Gateway/Web、Python业务owner、Backend重启、Windows实机或全部运行时收尾验收。

`test_browser_creation_intent.py`只暂扣真实RuntimePool的context回执，核原生Chromium已创建时磁盘先有created/unknown，再SIGKILL本fixture的Node并沿原磁盘冷启动。原close/delete不得以空context签核实终态，重复创建不得替换身份；另核拒绝不改变磁盘或原创建，释放原回执后同一资源仍能通过正式navigate新增页面、正常关闭并释放context。关键记录由正式HTTP生成，不补写恢复数据。定向pytest走主仓集成独占闸门、进程外超时和4GiB保护，期限包括排队；产物镜像本测试路径。不代表无身份接管、Python lease、Gateway/主Web或Windows，清理仅操作本fixture的精确进程实例。

`test_browser_storage_diagnostics.py`通过真实Node/HTTP/Chromium截图、原生目录冲突及日志writer的真实ENOTDIR次错，核对原失败操作、公开查询、冷磁盘记录和显式重试后的PNG字节；关键资源状态由正式创建／截图入口生成，故障文件仅制造fs边界冲突。suite `integration-browser-storage-diagnostics`固定60秒/4GiB；不代其它存储出口的网络验收、Gateway/Web、Python业务owner、Backend重启或Windows实机。

性能测试经 `browser_plugin_gateway_for_workspace` 启动正式 Gateway Browser 插件，再通过插件 HTTP/WS 路由测量资源治理、帧传输与进程树资源；插件 owner 重启后核对原 Browser 身份和页面状态恢复。`psutil` 遍历插件进程的子进程：CPU累计用户态和系统态秒数；Linux采样PSS，Windows/macOS采样USS，并记录指标名。

`browser_shutdown_operations.mjs`另经正式创建与交互生成两个真实页面，仅暂停其中一项原context释放：核整次关闭观察到期、peer的真实检查点和context终态、同Promise重入不重复释放、原执行结束后共享运行时关闭及两个原ID冷恢复表单。它验证协作式等待及关闭接管，不证明任意脚本可强制终止或生产进程退出；默认30秒与跨阶段预算由定向Manager单测核对。

## 可修改内容

`test_browser_operation_admission.py`经真实Session／ASGI资源API和Node／Chromium／HTTP验证动作等待原锁时准入到期、peer独立操作、原文件／lease不变和同入口重试；另核原`run`经受控网络完成后保持页面身份及表单结果。suite `integration-browser-operation-admission`固定90秒／4GiB，沿原集成闸门；准入后越过30秒预算的行为由单测推进时钟核对，不代Gateway／Web、Windows或整个shutdown验收。

- 浏览器资源、性能、帧治理和失败恢复集成场景。

## 不可修改内容

- 不要把测试 HTTP 页面服务描述为真实外部 E2E 依赖。

## 规范

- 工作区、端口和产物按测试文件隔离到 `out/tests/integration/workspace_services/browser/`。
- 工具测试由`integration_startup_mount`提供明确的工作区挂载；Browser 插件实例经工作区 Gateway 配置绑定，并通过精确的插件 HTTP/WS 路由访问。测试不得启动或推测 Browser 独立后端地址。
- 性能测试的页面HTTP服务在绑定后立即登记socket关闭；线程成功启动后再登记shutdown和join，避免启动失败时保留监听端口。停止、等待线程、关闭socket是独立释放动作，某一步失败或中断仍尝试后续动作，并保留原异常链。

同一版本排空文件还验证冷Python owner单次30秒等待：经正式创建及生命周期关闭得到资源，真实HTTP返回清单/授权版本后暂停回执，仅推进原截止；核同一页面、冻结版本、未知状态和同版本重试。它证明协作式恢复等待到期，不证明同步阻塞、真实网络永久挂起、独立Backend重启或整个operation/shutdown期限。
