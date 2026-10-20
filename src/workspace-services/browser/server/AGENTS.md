# 目录用途

`src/workspace-services/browser/server/` 存放可附加浏览器管理后端。它负责启动和管理 Playwright Chromium 页面、维护浏览器资源元数据、通过 HTTP/JSON API 与 WebSocket 提供 attach/detach 和远程屏幕交互能力。

```text
server/
├── backend.js                      HTTP、WebSocket 接线与传输授权
├── browserManager.js               工作区浏览器资源的创建、恢复和持久化协调
├── browserSession.js               单个浏览器页面与交互生命周期
├── workspaceIdentity.js            不依赖Manager的工作区UUID格式校验
├── url.js                         导航 URL 协议校验与缺省地址解析
├── browserStateStore.js            状态和产物落盘、检查点配额
├── operations/                    页面动作、输入分发与执行排队
│   ├── browserPageActions.js       DOM摘要、元素ref与工具动作
│   ├── browserInput.js             CDP指针、按键与文本输入
│   ├── browserOperationQueue.js    Session操作排队、序号与撤权检查
│   └── latestPointerMoveDispatcher.js  单连接连续移动合并
├── diagnostics/                   页面诊断与流统计，不管理业务或传输
│   ├── pageDiagnostics.js         网络请求实例关联、控制台记录和有界缓冲
│   └── streamMetrics.js           从帧观察/连接回执快照派生统计
├── transport/                     连接线格式、帧封包与逐连接流控
│   ├── protocol.js                输入校验、消息编码与共享帧封包
│   └── browserFrameFlow.js        最新帧、ACK 与 socket 积压
└── resources/                      浏览器运行实例、资源治理与检查点策略
```

页面负责产生帧，连接流控负责是否发送；不要把每个连接的 ACK 或待发帧状态放入共享页面。操作队列需要保留业务操作顺序，画面和指针移动则允许合并过时的中间值，二者不共用一套队列。

页面动作与排队细节见 `operations/AGENTS.md`。Session持有页面与动作所需状态，operations只执行明确委派的动作；连接封包和ACK仍归transport，Chromium及资源回收仍归resources。按执行、传输、资源生命周期区分目录，不按调用者是人还是Agent分两套实现。

操作队列显式持有`browserSession`资源宿主；串行等待用`serialOperationTail`，全部已接纳操作的等待用`acceptedOperationExecutions`及`drain()`。两者覆盖范围不同，具体归属见operations目录，不能用含混的owner或tail混指宿主、授权身份和并发排空。

工作区UUID格式校验归`workspaceIdentity.js`，Manager、后端入口与attach静态服务直接引用这个轻量模块。它不依赖Manager、Session或Playwright，不创建身份；调用方仍核对工作区权威身份文件，不能把格式合法当作归属匹配。

Manager 的状态文件保存只有一个写入者：同轮请求合并，写入期间的更新置为待保存，再取一份当前状态串行写入。所有等待方在待保存请求排空后才成功返回；快照生成或文件写入失败原样交给等待方，并释放在途 Promise，不能静默重试或让后续保存永久卡在旧失败上。合并的是完整状态保存，不合并业务操作或检查点。

Session的`toRecord()`显式组装恢复事实，供Manager写入状态文件；文档与操作序号、传输授权版本及最近运行时代际取当前宿主事实。`snapshot()`仍负责对外完整展示，其中设备配置列表、参与者、当前页面、保护派生和逐连接流统计不再为保存而计算或写入。恢复事实保留设备覆盖、下载身份、操作结果和生命周期时间；实时连接数在恢复后从当前句柄取得，不能用磁盘上的观察值恢复连接。

StateStore的`checkpointOperationTail`串行管理检查点读取、写入、删除和配额查询，不只是写队列；`checkpointSizeBytesByBrowserId`按浏览器ID记录磁盘文件字节数，包含JSON末尾换行。它只用于配额和目录大小变化检查，不能证明内容版本或检测同字节数的正文改写；不另建内容身份或版本来源。

操作与授权有两种独立版本：队列的 `operationRevision` 是排队时分配的操作序号，对外为 `operation_revision`，不表示完成顺序；`transportOwnerRevision` 是接纳操作时冻结的传输授权版本，从 Manager 的一次性 grant 传到连接、Session 和队列，异步操作前后均与当前 owner 版本比较。公开传输 API 沿用 `owner_revision`，Session 状态沿用 `transport_owner_revision`；内部不要用无修饰的 `revision` 混指这两种事实。

Chromium 实例代际统一用 `chromiumInstanceGeneration`：由运行时池在每次成功启动后递增，同实例的 context 共用；Manager 和 Session 的 `handleChromiumDisconnect` 只处理匹配的实例。它不是资源稳定 ID、传输授权版本或操作序号，池重建也不接续历史计数。公开状态 `runtime_generation` 和池诊断 `generation` 表达同一观察事实，不能单独作为跨重启身份。

Manager 的 `BrowserTransportGrant` 只保存一次连接所需的授权影子：稳定 `browserId`、冻结的 `transportOwnerRevision`、不透明领域关联和 `expiresAtUnixMs`。过期时刻统一使用 Unix 毫秒，与 Terminal 保持同一口径；公开 `expires_at` 仍为 ISO，30 秒有效期是持续时长，不能混作绝对时刻。内部标签页、运行时代际和业务会话 ID 不替代浏览器资源 ID。

`backend.js` 负责接入和路由，二进制帧编码及跨连接封包复用归 `transport/protocol.js`；连接的待发帧和 ACK 归 `transport/browserFrameFlow.js`。共享 schema 适配仍在上一层 `browser/protocol/`，不能在传输目录另建一套协议来源。

流统计也按这两层理解：Session 的 `BrowserFrameObservation` 记录 Node 的 Unix 毫秒观察时刻和 JPEG 正文字节，`fps/bitrate_bps` 来自以最新样本为终点的约五秒窗口，静默期间不刷新，不表示客户端显示率或线上带宽。`last_attach_first_frame_ms` 是最近一次 attach 开始到下一次帧观察的时差，不是首屏绘制耗时。帧线格式的 `timestamp` 则为 CDP 帧交换时刻或手动截图的 Node 观察时刻，单位 Unix 秒。

`diagnostics/streamMetrics.js`只从显式样本与连接快照派生统计，不持有Session、连接或窗口副本。样本采集、五秒窗口及首帧观察仍归Session；逐连接发送/ACK仍归FrameFlow。Session快照在组装时取得两层事实并调用唯一计算入口，不保留旧统计方法或第二套实现。

连接的 FrameFlow 用同一注入时钟计算发送到 ACK 的毫秒差，默认时钟为 `Date.now`；ACK RTT 包括客户端处理和排队。客户端上报的 decode/draw 是 `performance.now` 差值，draw 仅计 `drawImage` 调用；ACK 不等于该帧已显示。不要跨时钟源相减，也不要把帧源采样与各连接交付计数相加。

Session资源保护使用Node的Unix毫秒时钟：`lastNetworkActivityAtUnixMs`是按一秒节流登记的网络观察时间，`lastWebSocketActivityAtUnixMs`是最近一次WebSocket活动观察时间；持久字段仍为ISO。`softProtectionObservedAtUnixMsByReason`按保护码保存成功页面检查的观察时刻，五分钟租约从这里或WebSocket活动起算。`protectionInspectionFailureExpiresAtUnixMs`是检查失败硬保护的到期时刻，不是五秒重试时长或定时任务；`agentLockExpiresAtUnixMs`是从ISO锁字段解析的到期时刻。`*LEASE_MS/*RETRY_MS`才是持续时长，不能与这些绝对时刻混用。

页面诊断由 `diagnostics/pageDiagnostics.js` 独占记录与事件接线；Session 只在注册标签页时创建它，并通过同一入口读取和清空。诊断模块不持有 Session、不处理导航成败或资源活动保护，这两项仍归 Session。

网络和控制台各自有界；网络诊断跟随标签页生命周期：每页最多保留200行，按 Playwright Request 实例关联响应与失败，不按URL猜身份；记录编号在标签页内单调分配，清空不复用。弱关联在请求结束时释放，清空记录也断开旧关联。`started_at` 是 Node 观察到请求的时间，`finished_at` 是响应头或传输失败的最近观察时间，并非正文接收完成；`failed` 只表示传输失败，不能把HTTP错误状态或已收到响应头当作同一件事。

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
