# 目录用途

`src/workspace-services/browser/server/` 存放可附加浏览器管理后端。它负责启动和管理 Playwright Chromium 页面、维护浏览器资源元数据、通过 HTTP/JSON API 与 WebSocket 提供 attach/detach 和远程屏幕交互能力。

```text
server/
├── backend.js                      HTTP、WebSocket 接线与传输授权
├── browserManager.js               工作区浏览器资源的创建、恢复和持久化协调
├── browserSession.js               单个浏览器页面与交互生命周期
├── browserStateStore.js            状态和产物落盘、检查点配额
├── browserRuntime.js               运行常量、Chromium启动选项、工具超时与结果归一
├── browserDeviceProfiles.js        设备仿真几何、覆盖值与网络限速预设
├── workspaceIdentity.js            不依赖Manager的工作区UUID格式校验
├── url.js                          导航URL协议校验、缺省地址解析与ISO观察时刻
├── *.test.js                       对应模块的单元测试
├── operations/                     页面动作、输入分发与执行排队
│   ├── browserPageActions.js        DOM摘要、元素ref与工具动作
│   ├── browserInput.js              CDP指针、按键与文本输入
│   ├── browserOperationQueue.js     Session操作排队、序号与撤权检查
│   └── latestPointerMoveDispatcher.js  单连接连续移动合并
├── diagnostics/                    页面诊断与流统计，不管理业务或传输
│   ├── pageDiagnostics.js          网络请求实例关联、控制台记录和有界缓冲
│   └── streamMetrics.js            从帧观察/连接回执快照派生统计
├── transport/                      连接线格式、帧封包与逐连接流控
│   ├── protocol.js                 输入校验、消息编码与共享帧封包
│   └── browserFrameFlow.js         最新帧、ACK 与 socket 积压
└── resources/                      浏览器运行实例、资源治理与检查点策略
```

## 职责边界

页面动作与排队细节见 `operations/AGENTS.md`。Session持有页面与动作所需状态，operations只执行明确委派的动作；连接封包和ACK仍归transport，Chromium及资源回收仍归resources。按执行、传输、资源生命周期区分目录，不按调用者是人还是Agent分两套实现。

进一步定位：[页面动作与排队](operations/AGENTS.md)、[页面诊断与统计](diagnostics/AGENTS.md)、[连接传输与流控](transport/AGENTS.md)、[运行时与资源治理](resources/AGENTS.md)。跨边界问题先从本页查归属，再进入对应子目录。

## 身份、授权与版本

工作区UUID格式校验归`workspaceIdentity.js`，Manager、后端入口与attach静态服务直接引用这个轻量模块。它不依赖Manager、Session或Playwright，不创建身份；调用方仍核对工作区权威身份文件，不能把格式合法当作归属匹配。

Manager的`browsersById`以`browser_id`定位`BrowserSession`资源实例，包含仍保留的非运行记录；不按聊天Session或Thread索引。`/health`的`browser_count`直接读取该Map大小，与列表条目数量同义；它不是活跃Chromium或context数量。健康计数不生成、排序各资源的展示快照，运行时和治理器诊断仍由各自owner提供。

操作与授权有两种独立版本：队列的 `operationRevision` 是排队时分配的操作序号，对外为 `operation_revision`，不表示完成顺序；`transportOwnerRevision` 是接纳操作时冻结的传输授权版本，从 Manager 的一次性 grant 传到连接、Session 和队列，异步操作前后均与当前 owner 版本比较。公开传输 API 沿用 `owner_revision`，Session 状态沿用 `transport_owner_revision`；内部不要用无修饰的 `revision` 混指这两种事实。

Chromium 实例代际统一用 `chromiumInstanceGeneration`：由运行时池在每次成功启动后递增，同实例的 context 共用；Manager 和 Session 的 `handleChromiumDisconnect` 只处理匹配的实例。它不是资源稳定 ID、传输授权版本或操作序号，池重建也不接续历史计数。公开状态 `runtime_generation` 和池诊断 `generation` 表达同一观察事实，不能单独作为跨重启身份。

Manager 的 `BrowserTransportGrant` 只保存一次连接所需的授权影子：稳定 `browserId`、冻结的 `transportOwnerRevision`、不透明领域关联和 `expiresAtUnixMs`。过期时刻统一使用 Unix 毫秒，与 Terminal 保持同一口径；公开 `expires_at` 仍为 ISO，30 秒有效期是持续时长，不能混作绝对时刻。内部标签页、运行时代际和业务会话 ID 不替代浏览器资源 ID。

普通同步owner版本先撤销旧连接与grant，冻结原队列的已接纳集合并持久化新版本，再核排空结果；与关闭共用默认5秒预算。超时明确返回`browser_owner_revision_drain_timeout`/408，原操作和运行时仍保留，新版本不回退，也不宣称旧副作用停止；原操作最终结束时仍按冻结授权拒绝旧结果，同版本重试只等待原集合，不重放动作。该等待预算不覆盖持久化阻塞、Python首次恢复gate或整个operation期限。

管理stop/delete的版本推进必须显式传入`for_stop=true`：先撤销旧连接/grant、关闭新操作准入并持久化新版本，此准备步骤不等待原执行；随后由原close链释放context再排空同一队列并核实终态，避免原脚本等待context关闭时互相等待。准备写盘失败保持准入关闭并传播原错误，不发布停止成功。普通恢复、attach/detach及grant同步均显式传false并保留原排空语义；内部HTTP缺少boolean意图直接拒绝，不增加旧协议fallback。

Manager关闭先关闭新创建准入，并等待本次已进入创建链的配额检查、初始导航和最终状态保存；创建与原交互排空共用一次默认5秒单调预算。未接纳创建返回绑定原intent的503/not_created，重复身份仍按原冲突拒绝；已登记Session含created状态在关闭入口同步关闭操作准入；已接纳创建超时保留原Promise、资源和共享运行时，迟到完成再由同一Manager重试检查点。不因创建等待超时取消启动或重放创建。启动失败直接复用Session.close的唯一资源释放/检查点删除/终态保存链；失败状态保持failed/unknown，未核实创建收尾阻止共享池关闭，用户可由原close/delete核实。该等待不是整个shutdown期限，生产信号退出协调与挂起操作终止仍TODO。

## 页面执行、仿真与生命周期

操作队列显式持有`browserSession`资源宿主；串行等待用`serialOperationTail`，全部已接纳操作的等待用`acceptedOperationExecutions`及`drain()`。两者覆盖范围不同，具体归属见operations目录，不能用含混的owner或tail混指宿主、授权身份和并发排空。

工具超时只结束请求等待。原脚本及页面reset继续由同一队列登记；排空期间新增的执行句柄也在原单次预算内复核。stop/delete关闭context后仍须排空原执行，超时返回`browser_close_operation_drain_timeout`/408并持久保留unknown；原执行结束后显式重试完成原检查点删除和终态发布，context不重复关闭。这不证明不合作脚本可强制终止，也不是整个operation/shutdown期限，Python业务owner的挂起接管仍TODO。

设备与网络仿真参数统一由`browserDeviceProfiles.js`解释，Session负责应用到CDP。`baseViewport/baseScreen`已应用设备方向，但不含自定义覆盖，尺寸为CSS像素；截图物理尺寸还需乘DPR。网络预设的单位及`-1`不限速语义写在`BrowserNetworkThrottleProfile`类型处，不能把字节吞吐当比特率或把响应头延迟当整次页面加载耗时。

浏览器资源的`created_at`来自Manager接纳创建时的`browserCreatedAtIso`，早于该资源的`session.start()`完成；新资源也用该值初始化用户交互的空闲计算基线。下载的同名字段则属于`BrowserSavedDownload`，在`saveAs`成功后组装记录时生成，来源`url`不等于附件访问URI；保存文件成功与下载记录持久化完成是两个时刻。新增时间字段应在产生它的owner及类型处说明观察点，不能仅凭`created_at`推测业务含义。

Session资源保护使用Node的Unix毫秒时钟：`lastNetworkActivityAtUnixMs`是按一秒节流登记的网络观察时间，`lastWebSocketActivityAtUnixMs`是最近一次WebSocket活动观察时间；持久字段仍为ISO。`softProtectionObservedAtUnixMsByReason`按保护码保存成功页面检查的观察时刻，五分钟租约从这里或WebSocket活动起算。`protectionInspectionFailureExpiresAtUnixMs`是检查失败硬保护的到期时刻，不是五秒重试时长或定时任务；`agentLockExpiresAtUnixMs`是从ISO锁字段解析的到期时刻。`*LEASE_MS/*RETRY_MS`才是持续时长，不能与这些绝对时刻混用。

Manager关闭时，先同步关闭当前整批运行资源（含已冷回收资源）的操作准入，再通过队列唯一的`drain()`等待各资源完整已接纳集合；本批共用默认5秒的单调截止，后续资源只使用剩余预算，不为每个资源重新计时，覆盖串行排队及并发准备/执行；不能由`discarded`或在途计数为零跳过。单次定时预算覆盖整份集合，不随操作阶段重置，也不受系统校时影响；它不是资源租约或持久时刻。Session排空超时在清理连接、解锁与检查点转换之前明确报错，原操作仍由队列持有；排空后仍为冷状态的资源保留原检查点。Manager收集排空超时后继续处理其它资源，但不将超时资源改成lost、不删除其检查点，也不关闭共享运行时池；汇总原错误后明确失败，原操作结束后可沿同一入口重试。检查点失败后的收尾若再次失败，Manager按资源保留原采集错误与收尾错误，继续其它资源，但不关闭共享池。Session只在context释放、检查点删除和状态保存完成后落为lost；失败保持原status与unknown，保留原句柄供同入口重试，unknown保存也失败时保留两份异常。整个关闭期限、其它失败收尾和挂起操作终止核实仍由后续合同推进。

## 恢复记录与落盘

Manager 的状态文件保存只有一个写入者：同轮请求合并，写入期间的更新置为待保存，再取一份当前状态串行写入。所有等待方在待保存请求排空后才成功返回；快照生成或文件写入失败原样交给等待方，并释放在途 Promise，不能静默重试或让后续保存永久卡在旧失败上。合并的是完整状态保存，不合并业务操作或检查点。

Session的`toRecord()`显式组装恢复事实，供Manager写入状态文件；文档与操作序号、传输授权版本及最近运行时代际取当前宿主事实。`snapshot()`仍负责对外完整展示，其中设备配置列表、参与者、当前页面、保护派生和逐连接流统计不再为保存而计算或写入。恢复事实保留设备覆盖、下载身份、操作结果和生命周期时间；实时连接数在恢复后从当前句柄取得，不能用磁盘上的观察值恢复连接。

StateStore的`BrowserStateWriteSnapshot`只承载Manager本轮恢复记录，与页面检查点和展示快照分开；`stateFilePayload`补充权威工作区身份与文件schema。顶层`updated_at`在Manager开始组装本轮快照、逐条读取`toRecord()`之前生成，不是各资源的更新时间，也不表示写盘完成；类型直接引用Session的记录返回值，不复制一套字段定义。

`browsers.json`读取/解析/记录核验及写入/发布失败按workspace身份、artifact、阶段和错误码生成公开错误，完整原生位置及异常只留在日志和cause；只有实际正文读取的ENOENT表示尚无状态。写入失败后只删除本次唯一临时文件，保留已发布旧字节；清理也失败时cause为按发生顺序保留写入/清理异常的AggregateError，外层code仍归写入阶段。下一次显式保存不扫描或删除其他尝试的文件。

StateStore的`checkpointOperationTail`串行管理检查点读取、写入、删除和配额查询，不只是写队列；`checkpointSizeBytesByBrowserId`按浏览器ID记录磁盘文件字节数，包含JSON末尾换行。它只用于配额和目录大小变化检查，不能证明内容版本或检测同字节数的正文改写；不另建内容身份或版本来源。

检查点读取、写入的错误包装通过`cause`保留原始解析或文件系统异常。写入与临时文件清理都失败时，`cause`为按发生顺序保留两份原异常的`AggregateError`，对外错误码仍归写入阶段；清理错误不能覆盖主失败。失败不更新大小索引、不替换已发布正文，队列释放后仍接受下一次显式操作。`browserStateStoreErrors.test.js`分别验证真实文件读取错误与显式注入的发布/清理故障，不以这些故障注入代替产品恢复验收。

检查点目录扫描每批最多处理64个目录项，批内并发读取大小或清理崩溃临时文件，再进入下一批；不按文件总数一次创建全部异步任务。仍完整核验目录和配额，不以缓存命中跳过外部变更检查。`readdir`仍返回全部目录项，大小Map仍随检查点数增长；这不是整个扫描恒定内存的保证。

## 画面交付与观测

页面负责产生帧，连接流控负责是否发送；不要把每个连接的 ACK 或待发帧状态放入共享页面。操作队列需要保留业务操作顺序，画面和指针移动则允许合并过时的中间值，二者不共用一套队列。

`backend.js` 负责接入和路由，二进制帧编码及跨连接封包复用归 `transport/protocol.js`；连接的待发帧和 ACK 归 `transport/browserFrameFlow.js`。共享 schema 适配仍在上一层 `browser/protocol/`，不能在传输目录另建一套协议来源。

流统计也按这两层理解：Session 的 `BrowserFrameObservation` 记录本进程的单调毫秒观察点和 JPEG 正文字节，`fps/bitrate_bps` 来自以最新样本为终点的约五秒窗口，静默期间不刷新。采样、窗口淘汰和`attachRequestedAtMonotonicMs`统一使用`performance.now()`并向下取整到毫秒，保留原有整毫秒跨度与首帧耗时精度；系统校时不改变统计，这些进程内起点不落盘。`last_attach_first_frame_ms`仍为最近attach开始到下一次帧观察的毫秒差，不能当作客户端绘制耗时。帧线格式的`timestamp`继续保留CDP帧交换时刻或手动截图的Node观察时刻，单位Unix秒；不得与单调观察点相减。

`diagnostics/streamMetrics.js`只从显式样本与连接快照派生统计，不持有Session、连接或窗口副本。样本采集、五秒窗口及首帧观察仍归Session；逐连接发送/ACK仍归FrameFlow。Session快照在组装时取得两层事实并调用唯一计算入口，不保留旧统计方法或第二套实现。

连接的 FrameFlow 用同一单调时钟计算发送到 ACK 的毫秒差，默认时钟为 `performance.now`，避免系统校时污染耗时；ACK RTT 包括客户端处理和排队。客户端上报的 decode/draw 是客户端自己的 `performance.now` 差值，draw 仅计 `drawImage` 调用；ACK 不等于该帧已显示。不要跨时钟源相减，也不要把帧源采样与各连接交付计数相加。

## 页面诊断与采集

页面诊断由 `diagnostics/pageDiagnostics.js` 独占记录与事件接线；Session 只在注册标签页时创建它，并通过同一入口读取和清空。诊断模块不持有 Session、不处理导航成败或资源活动保护，这两项仍归 Session。

网络和控制台各自有界；网络诊断跟随标签页生命周期：每页最多保留200行，按 Playwright Request 实例关联响应与失败，不按URL猜身份；记录编号在标签页内单调分配，清空不复用。弱关联在请求结束时释放，清空记录也断开旧关联。`started_at` 是 Node 观察到请求的时间，`finished_at` 是响应头或传输失败的最近观察时间，并非正文接收完成；`failed` 只表示传输失败，不能把HTTP错误状态或已收到响应头当作同一件事。

`debugSnapshot()`的DOM投影仍由Session执行：最多下探5层，每个元素直接读取前20个属性和前80个子元素，不先复制全集合。属性值、直接文本分别截取300和240字符，`node_id`按原始子元素索引生成。直接文本按子节点原序读取词段并用单空格连接，保持原有空白归一与UTF-16截断；取得240字符后不再读后续节点，也不拼接全部正文。全空白或正文靠后的输入仍需扫描前面的节点和空白。来源按性能条目原序筛选、按URL保留首个有效记录，取得200个后停止组装与去重；浏览器API仍先返回性能条目数组，不能把这些前缀优化当作整个快照成本恒定的保证。

# 可修改内容

- 可以新增或修改浏览器 manager、HTTP 路由、WebSocket 协议处理和状态持久化逻辑。
- 可以维护测试友好的浏览器启动、页面摘要、截图和工具动作实现。
- 可以调整浏览器状态 DTO 字段，但要同步 Python 资源视图和前端 attach 页面。

# 不可修改内容

- 不要把主聊天前端组件放到这里。
- 不要直接依赖 FastAPI 容器或 Python 服务。
- 不要把浏览器状态持久化到 `.boxteam/browser-manager/` 以外的位置。

# 规范

- 使用 Node.js ESM。
- 导航先识别显式协议，再由同一白名单校验；不支持的协议明确拒绝，不补成另一个 HTTP(S) 目标。只有裸主机地址补缺省协议，主机后的数字端口与协议段分开识别。
- 失败时返回明确 HTTP 错误或 WebSocket error 消息。
- `transport/browserFrameFlow.js` 的所有新帧先替换唯一待发帧，再由同一发送入口检查 ACK 和 socket 积压；socket 恢复后新帧不能留下稍后重放的旧待发帧。
- detach 只断开客户端，不关闭浏览器；close/delete 才关闭页面。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
