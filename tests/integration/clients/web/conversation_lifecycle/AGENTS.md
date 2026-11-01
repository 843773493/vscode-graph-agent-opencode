# 目录用途

Section 8 完整会话生命周期的唯一 Python owner、浏览器驱动和独立历史 oracle。通过正式 Workbench、真实 Gateway／Backend／SSH 和精确模型回放验证聊天、子 Thread、fork、恢复及跨工作区协作，属于 Integration。

# 可修改内容

- 唯一 pytest 场景、scope fixture、focus collection preflight、真实浏览器操作及只读历史断言。

# 不可修改内容

- 不复制另一套完整聊天场景，不伪造产品 UI、业务状态或 Provider 成功响应。
- 不放宽 CAS、owner、历史源坐标、模型请求及原产品期限断言。

# 规范

- Python owner、focus CLI及诊断自检统一通过Node／`--expose-gc`执行唯一MJS驱动；矩阵和依赖仍使用Bun。Bun/JSC在同一4GiB数据段保护下预留大量可写映射，不能以扩大上限、缩小完整场景或自动运行时回退掩盖失败。导航operation及barrier身份统一使用既有受控identifier工厂的独立namespace，保留UUIDv7格式；只在诊断内存自检中显式GC。
- `section8_ports` 仅在本文件获分配的端口块内平移服务布局，持续持有同一组租约并将选定布局传给三工作区装具；只跳过真实地址占用，保留探测记录，不终止已有监听者或扩展端口块边界。
- Gateway 崩溃回执核对 `gatewayWorkspaceId` 路由身份，不读取已删除的 `workspaceId` 字段；失败诊断通过日志路径取大小，必须支持故意重启后已关闭的旧日志句柄，不能覆盖原浏览器失败。
- 正式工作区与产物镜像本测试路径；控制端、配置、临时目录和日志按 run／node 隔离。
- 长流程诊断正文按原响应的 X-Request-ID、URL、方法及状态绑定 CDP request，等原请求完整结束后读取；不得用额外 HTTP 请求补造正文，也不得让 Playwright 长期缓存完整历史。读取失败仍记录在原 fact，保留 owner 与错误分类断言。
- CDP 在途请求在收到响应或失败时释放；响应表只保留业务 API 和仍需原始正文归因的 502，不累计成功静态资源。页面错误、请求失败和 console 错误仍完整审计，不能按资源类型忽略未知错误。
- 复用 web_workbench_assets 构建，main／child 都通过原版 Thread EditorPane 和精确工作区／Session／Thread 定位。
- ShadowRoot 探针必须从真实 Thread 内节点读取；先核正向节点存在，再作结束或数量断言。
- 历史遍历按完整 owner oracle 的 Turn 身份、root logical ordinal 和同一 DOM 快照的节点序列确认窗口偏移，不将完整历史索引当作当前窗口的 `data-index`。安全通知正文重复且没有唯一序列证明时拒绝猜测；活动详情失败时保留原按钮连接状态及阶段快照，不降低完整 parts／顺序／坐标断言或增加期限。
- 校准可在导航确认后重建同一Turn的实时壳；身份阶段在原60秒预算内同时等待完整owner、可见性与精确root坐标，不把只有Turn ID的临时DOM作为持久历史已就绪，也不延长后续内容断言期限。
- focus 仍由 MJS 唯一 registry 定义；pytest collection 在启动 fixture 前校验，不保留旧 E2E 别名。
- `copy_capture_recovery` 只复用完整 forks 中的 capture／marker 恢复场景，创建独立保留会话后执行同一业务函数与错误审计；用于定向重验，不代替完整 forks 验收。
- `copy_attachment_deletion` 只复用完整 forks 的附件 finalizer／目标删除竞争函数；reservation 只创建 claim，未提交目标关联时须精确核不存在，提交先行则删除后核 released。两种顺序都保留原 claim／冻结来源、结算、lease、重启不复活及来源内容断言；定向结果不替代完整 forks。
- copy 请求页在重启后须恢复存活观察目标，按 Session、Thread 和过滤范围匹配真实 connected；仅当重启前已接受的 source DELETE 在同页完成原 journal、目录／聊天宿主和原版 Tab 释放证明后，才验证该精确 source Trace 释放。关页与最终错误审计共用同一归属检查，不能用删除证明豁免其它存活订阅。
- 重启期间工具审批轮询的 502 须精确绑定 source Session／Thread、原页面和实际 Gateway 回执；目录对账通知按真实响应头的 request_id 消费已通过网络审计的同页 snapshot／events／navigation-events 或精确 Session GET 响应，不能重复消费或按文案、时间猜配并发请求。
- copy 请求页面先等待原版 `code/didRestoreEditors`；重启前绑定实际 Thread EditorPane 的工作区、Session 和 Thread owner，并将原版 Tab 的 Thread basename 唯一关联到真实成功的 bootstrap 回执，冻结尚未挂载的恢复标签身份。回执须核同 Gateway origin、路由、业务工作区、Session／Thread 与 request_id；歧义、缺失回执或身份错配必须失败，不得仅凭失败 URL 放行未知 Session。成功 bootstrap 原件随失败诊断保留。
- 删除证明须等待当前文档的所属 Tab、聊天宿主和在途会话读取收敛；分类强制同页面和文档，不得借用另页或前一文档的证明。附件 copy 页面与前台页同时观察 source 删除时，各自在关页前以同一原操作重新核 journal／终态、DOM／标签和读取收敛；这不证明后续文档恢复正确。
- 删除证明的响应正文采集须位于最后一次在途读取检查之前；正文等待期间新到达的读取必须排空后再冻结证明时间，不能把仍在途的响应归入已完成证明。
- 删除排空重启前关闭的文档，须在原文档中以成功 bootstrap 核原版 Tab 的完整 owner，并冻结同一删除 journal 的 Session 集合；终态后再结合该文档真实 pagehide→close 回执证明释放。只归因关闭前的精确拒绝响应，不借重开文档的 DOM 证明，不接受仍存活文档或其它删除 batch。
- 删除提交及排空场景分别核对目录隐藏与原生 Thread 标签生命周期，不把所有 data-session-id 节点都当成目录项；正常关闭页面后重启 Backend 时等待工作台保存展示态，再验证删除标签关闭且独立保留会话仍可操作。删除排空失败通过原版通知核操作 ID 和错误详情，不能依赖旧状态栏或 console。
- `native_shutdown.mjs` 统一采集和审计原版关闭日志。四个已知 shutdown joiner 须绑定同一页面／文档的真实关闭事件：刷新采用同一采集序列中的顶层 beforeunload→pagehide→导航完成回执，关闭日志可发生于 pagehide 回调内；直接关页采用顶层 pagehide→Playwright close 回执。不用毫秒容差代替事件顺序，不采集 iframe 生命周期作为工作台关闭证据。RPC 取消还须有紧邻实际 console 调用的原 Error 对象身份、完整固定上游 dispose 调用栈及同文档 stopExtensionHosts 关闭日志；不同对象独立核验，同一对象重复输出仍失败。观察器不得吞日志或改写错误，未知 joiner、存储写入失败及业务异常仍须审计。
- Python 的 consoleIndex 总审计必须纳入浏览器返回的原版关闭日志类别，并保持每条原始错误恰好归类一次的完整索引断言；不能漏算已有类别，也不能忽略未归类错误。
- console 观察器会改变浏览器报告的调用位置，必须同步采集首个真实调用帧，按同文档紧邻序列恢复来源资源及行列并保留 observerLocation 原件；不得猜测来源 URL。无法解析的来源须明确留下 callSiteError，不能用关闭类别放行。真实 Chromium 对照检查应核有无观察器时所有错误正文及原位置完全相同。
- 同文档并发旧历史读取可以由一个在途 bootstrap 校准：bootstrap 请求不得早于被拒读取的发出，其成功响应不得早于拒绝响应，后继 tail 必须在 bootstrap 成功后发出；仍须同工作区／Session／Thread、epoch、request_id 和刷新前完整 DOM 证明。不按延迟容差豁免，也不能借用其它文档或 Thread；失败回执保留 live／导航证明和活动检查快照。
- MCP 激活按现行 v3 冻结 lineage、完整成员摘要和精确连接代际核执行身份；503 核完整公开回执，并以同一 request_id 的真实 Backend 日志核原始 RPC 拒绝，不要求公开出口复制远端自由正文。
- 团队拒绝分别核对 Job 的公开 I/O 失败和原工具调用的公开 tool-execution 失败；两层均以同一准入 request_id 的唯一日志核精确 Thread 原始拒绝，保留 failed Job／Turn、canonical 工具身份、无 final_text 和跨 Session 零副作用断言。
- 通信删除的 discovery 拒绝分别核 builtin 工具失败和 Job 的 path／status／code／request_id；精确目标 Session 与完整原始响应必须从同一准入 request_id 的唯一 Backend 日志核对。extension read 的 ToolException 保留业务说明，不能套用 builtin 失败格式；删除观察仍核原持久页、失败终态、无后继调用和零副作用。
- 导航故障注入的原版通知按原操作 ID、完整后端回执及 request_id、页面／文档和真实通知 DOM 归因；本地 abort 还核无后端入队、无 outbox／目录节点。repair 通知必须绑定持久失败回执与同一删除 journal 的实际恢复；删除后的 404／409 及对应通知必须有原 HTTP 正文和删除完成、真实 DOM 消失的后续证据。每条原始错误恰好分类一次，未知错误及重复通知仍失败。

- required 附件原件缺失的 fork HTTP 500 核现行结构化公开摘要，并按同一 request_id 的唯一 Backend 错误及完整异常链核具体缺失原因、精确 Session fork POST；保留 capture／creation aborted、无目标／claim、来源事实不变与 optional 不可用附件不复活的断言。缺失场景进度及原始响应／诊断在失败 JSON 中保留，不以修改公开文案代替产品错误验证。
