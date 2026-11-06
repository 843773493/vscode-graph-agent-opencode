## Context

本稿从 `add-workbench-quick-access` 的缓存设计直接迁入。已有源码及原任务收据保留，不因拆分而要求重新实现。任务编号 17／18 仅用于追溯历史证据，不表示排期。

## Goals / Non-Goals

目标是逐条闭合尚未验证的缓存消费者、生命周期与持久复用。缓存不成为业务事实源，不扩大现有恢复保证，不建立 CacheManager 或额外原生能力。快速访问只消费已具备的原能力，不等待本变更全部完成。

## Decisions

### 分层缓存与持久数据的统一职责清单

这里统一的是合同、接入检查与诊断，不是统一存储或缓存引擎。VS Code 没有一个包办全部缓存的服务；项目保留原服务的生命周期和失效入口，各层只保存自己可由权威来源重新取得的数据。未保存内容及用户配置不是缓存。不得为管理方便建立 CacheManager、跨 owner 字节库、全局缓存数据库或新原生适配层；也不要求每一层都持久化、都支持离线、都以 URI 字符串为 key。

| 层次 | 原服务／实际 owner | 身份、版本和生命周期 | 本项目接入与证明边界 |
|---|---|---|---|
| 文本及只读模型、Diff 模型 | `ITextFileService`／TextFileEditorModelManager、`ITextModelService` resolver／引用、原 Diff 输入 | 完整规范资源投影；可变模型自己的 version；不可变来源另带原 revision／before-after；引用和 working-copy 生命周期 | 同资源分屏复用模型而视图态独立，并发 resolve 不重复建模；dirty 内容不被远端刷新覆盖。干净模型可释放，不额外把所有正文持久镜像进 IndexedDB |
| 文件 stat／目录观察与 watcher | 原 FileService、目标文件 Provider、原导航 owner | 稳定 owner／根／规范资源及其观察值，变化、重连／overflow 重新核实 | FileService 的 stat／ETag 机制不等于通用 stat 缓存。清点实际 Provider 是否缓存；没有则保留轻量真实请求，不先造缓存。失效观察值不能代替保存基线或永久缓存 not-found／无权限 |
| 文件／媒体／Webview 字节 | 浏览器 HTTP 缓存、原 Webview Cache Storage、原 raw 与资源读取 owner | origin／profile、认证及完整目标；validator 再验证，生命周期按下节 | 覆盖文字原字节、图片、Webview 的 CSS／字体／脚本等已允许资源，不限图片；附件、只读来源与 Diff 仍沿各自能力／版本，不能全部改走普通文件接口；Range 不作为完整缓存 |
| 搜索交互与候选 | 原 ISearchService、FileSearchManager session／clearCache、FileQueryCacheState、fuzzy scorer 与各搜索 Provider | 已选范围、模式／选项／权限代际及 Provider 版本；每次输入新请求，连续交互按原能力复用 | 沿第 16 组缓存／cursor 分离、受限预热与释放合同，不新增本地联邦缓存或以缓存遗漏报告完整无匹配 |
| 导航已加载页、目录镜像及领域索引 | 原导航模型、catalog revision、Gateway 原公开目录同步、各领域索引 owner | 分别保留真实 scope、独立 revision／cursor、来源／更新时间／stale | UI 已加载树不是全量目录，Gateway 镜像不是权威或正文索引。身份／revision 变更后撤销不适用页，不能全盘扫描业务目录重建；索引由原 owner 决定是否建立和怎样重建 |
| 聊天历史展示镜像 | 既有 threadHistoryCache／浏览器展示态 owner、canonical history owner | Gateway／workspace／Session／Thread／用户边界、镜像格式及历史版本 | 沿现有先显只读镜像再同步的合同，后端新快照替换展示镜像；精确定位临时原文不写入镜像。与文件缓存的“验证失败不能显示当前成功”区分，不移除既有带状态的历史预览 |
| 配置派生缓存 | 原 ConfigurationCache 与配置／profile 服务 | 上游 type／key、资源 scheme／真实 profile，配置变更和版本决定有效性 | 已持久的 `workbench:` 本地设置文件不重复缓存；真实非本地资源按原 needsCaching／read／write／remove 接入。不将 Workbench 容器 workspace ID 当作后端业务工作区，不另存一份权威 settings |
| 词法、语法、符号与语言 Worker | 原模型 tokenization、实际语言服务／Worker 及其索引 owner | 模型 URI／version、language ID／语言配置与依赖环境；按原服务引用／dispose／restart | 文本变化即失效对应分析，迟到旧模型结果不可写回。仅在真实服务支持业务 URI／查询范围时开放；未启用的工作区符号、语义或远端语言服务不能凭“缓存已有”绕开 scope，不新造 LSP 或全局代码索引 |
| 扩展扫描／清单／语言包等派生数据 | 实际启用的原 Web extension scanner／服务 | 原 profile、扩展身份／版本、产品／语言版本；沿安装、卸载及升级失效 | 只清可重建扫描／解析结果，不删除实际扩展／语言包资源、安装记录或用户启用状态。条件接入既有 Web 能力；Electron 容器不导入原生 extension scanner、插件下载体系或新增语言包下载能力；没有运行的服务明确不适用 |
| 发行静态资源、Worker 与引擎代码缓存 | 同一 Workbench 构建／server factory、浏览器／Chromium | 构建输出内容地址及宿主版本；网络缓存与引擎缓存各自管理 | 入口、未带内容 hash 的 main／Worker 使用再验证；仅实际内容寻址资源可长期 immutable。开发沿 Vite/HMR，不缓存旧 bundle；OAuth 回调保持 no-store。Electron 仍跑相同 Web renderer，不为取得 CachedData 导入原生 VS Code bootstrap |
| 受保护恢复数据，非可淘汰缓存 | 原 WorkingCopyBackup／History 服务、Composer 草稿、输入序列化和原持久存储 | working-copy／资源／版本、profile／用户及每编辑器组原归属 | 未保存备份、本地文件历史、草稿、编辑器恢复／设置／快捷键／信任与凭据按原保留及显式删除合同管理，禁止加入缓存淘汰。Electron 继续使用 browser Workbench 的服务，不能因为桌面宿主而悄悄换 native 服务 |

#### 各层共享的有效性与恢复边界

每层实施前在原 owner 记录实际消费者、缓存对象、身份／版本、存储、失效入口、释放／配额和可核验场景；矩阵是这份清单，不创建运行时注册平台。不适用或原服务无缓存时明确记录，不为凑齐缓存类别新增持久存储。URI 的唯一投影隔离 Gateway／workspace，Thread 还保留 Session／Thread；模型代际、查询代际、配置版本、HTTP validator、owner cursor 和业务内容 revision 不互换。权限／身份变化、scope 切换、退出及取消撤销旧结果的准入，迟到异步完成不能复活旧代际或跨目标写入缓存。

模型共享与视图恢复分别处理：同一可变文件在两个编辑器组中拥有一个正文 working copy，各组的光标／选择／滚动独立；Diff／只读历史版本按原输入身份隔离，不能因路径相同复用成“最新文本”。最后一个使用者释放时遵守 resolver／editor 引用及 dirty／备份规则，不能按 LRU 或关闭一个 Tab 删除另一个仍在用的模型。内存模型命中可先显示，原版异步 reload／watch 按其选项继续；没有模型或恢复数据时走正常读取，不能承诺所有干净文件刷新后先显示持久缓存。需要磁盘字节复用时只消费下一节的原 HTTP／Webview 层。

静态缓存沿实际构建清单和唯一 server factory 接入：当前 `/out/main.js` 不是内容地址，不能仅因是 JS 就长期 immutable。入口及无 hash 资源通过有效 validator 再验证，有 hash 的字体／图片／分块可沿内容地址复用；无内容 hash 的 Worker 同样不能无限保鲜。升级切换以匹配当前构建的入口、bundle 和 Worker 为准，缺资源明确失败，不返回旧资源冒充新版本。原 Webview 隔离宿主／Worker 按固定版本部署；不新增缓存整个工作台的离线 Service Worker，不改变开发 HMR 语义。浏览器／Chromium 的编译缓存由引擎自动管理，项目不指定其磁盘格式、文件名或必然命中率，也不把冷启动耗时单独当作缓存正确性证据。

重启时持久数据保持原格式版本／owner 校验与一次迁移合同：先恢复允许恢复的布局、备份或明确只读历史镜像，再沿原 owner 同步；业务状态和执行能力不能从旧缓存恢复为“已验证”。普通文件字节使用前仍校验，干净模型、fuzzy scorer、语言 Worker 等内存缓存可重新建立，不强求落盘。更新应用仅淘汰不兼容的可再生缓存，不能清空 profile、IndexedDB、浏览器站点数据或全部 Storage 来解决格式不匹配。新版本不能读取的受保护恢复数据保留原件并明确报告，不能默默丢弃或用新空草稿替代。

#### 缓存诊断与清理入口

复用原命令面板、设置、输出／日志及各 owner 已有释放入口，不新增缓存管理页面或统一后台服务。需要补充的清理命令只针对具名可再生类别和明确作用域，执行前固定原 owner／profile／workspace，列清实际影响；“移除最近打开”“关闭编辑器”“删除草稿／备份”“删除会话”仍是独立操作，不包装成清缓存。不能将 Electron clearStorageData、删除 userData 或浏览器清站点数据当产品清理实现。浏览器 HTTP 缓存未提供选择性清除 API 的场景使用原标准 reload／再验证或引擎淘汰，不为清理方便新增原生 bridge；不虚构一个 Web/Electron 都有的清所有缓存按钮。

只在相关原诊断中区分内存命中、已再验证字节复用、缓存未命中、明确 stale 镜像、主动绕过、未支持和存储失败，并保留适用的实际 scope、版本、数量／字节／耗时与当前 request_id；无网络读取时不伪造请求 ID。缓存命中不得成为业务操作成功的证据，业务 API 返回仍完全替换镜像。存储／读取失败、权限拒绝及损坏不能吞成成功缓存命中；可丢弃条目由原 owner 明确失效后重读来源，受保护恢复数据保留并报错。官方 Webview Worker 的异步缓存写入沿用下节的上游行为，不额外要求失败诊断。复用已有本地诊断，不新增遥测、统一计数数据库或跨工作区后台探测。维护有界引用／条目／缓冲或引擎配额，避免无限模型保活、全量搜索预热及无关工作区扫描。

### 资源字节缓存复用原版加载链

本节覆盖已允许读取的普通文件字节和 Webview 资源，图片只是验收样例之一；不替代上表中的模型、备份、历史、配置或搜索缓存。固定上游的 `mediaPreview/browser/imagePreviewEditor.ts` 是 `FileService.readFile → Blob → object URL`，并不经过 Webview；`webview/browser/pre/service-worker.js` 才有 `vscode-resource-cache-*` 的 Cache Storage 与条件校验。不能用“原图片已解码”或“Electron 有 profile 目录”证明两者已完成持久缓存。此前图片／Range 证据继续有效，但不覆盖本节新增要求。

| 资源入口 | 唯一缓存 owner 与读取路径 | 明确边界 |
|---|---|---|
| 原图片编辑器及普通原文件预览 | 原 FileService／文件 Provider → 原 raw API；完整 GET 复用浏览器／Chromium 私有 HTTP 缓存 | 保留原图片 Pane、Blob 与 object URL 释放，不新增图片编辑器或文件下载镜像 |
| 原 Webview 的受控文件资源 | 原 `asWebviewUri` → Service Worker → 工作台 `loadLocalResource` → 同一 FileService；复用上游 Cache Storage | 保留 URI 投影、localResourceRoots 和原访问校验；不复制 Service Worker 或再写一个缓存状态机 |
| 外部 HTTP(S) 图片 | 沿原浏览器请求、来源响应头及 Webview 安全规则 | 不隐式下载到后端，不把外部 URL 登记为工作区文件，不承诺所有第三方资源都能缓存 |
| 音视频 Range、显式下载、临时归档及错误响应 | 沿原读取／下载链；Range 保持 206／Content-Range 及原取消 | 不把分段写成完整资源缓存，不为预热下载完整大文件；下载与临时数据仍保持 no-store |

普通工作区文件预览的 raw 完整 GET 选择 `Cache-Control: private, no-cache` 与 ETag，替换该场景当前的 `no-store`；这是明确的响应缓存策略变更，不改变文件字节、权限、URI grammar 或保存合同。`no-cache` 表示允许私有存储但复用前必须验证，不是离线或免校验读取。原显式下载、临时文件、Range 和失败响应仍为 no-store，其它 API 不顺带放开。前端只在该原入口沿标准 fetch 缓存语义读取，不新增 fetch 缓存包装器；Gateway、Electron 回环代理保持 If-None-Match、ETag、Cache-Control、Vary 和 304 的真实 HTTP 语义，不集中持有图片缓存。

每次校验先沿原认证、稳定目标路由及文件 owner 校验当前资源可访问，再比较 validator；相同返回空正文 304，不重新读取或传输图片正文，浏览器以旧字节组成可解码响应；不同返回 200 和当前字节。validator 绑定稳定工作区、规范资源身份及原 owner 的当前版本观察值，不能只用显示路径或临时 route。HTTP Vary 明确覆盖原目标工作区／后端身份请求头，以及该入口实际使用的认证头／Cookie，并正确合并已有 Vary；同一 URL 的不同目标或认证上下文不能互相命中。业务 URI、显式缓存标识及日志不嵌凭据。304 保留适用 validator、缓存策略、Vary 及本次网络请求的 request_id，不把缓存字节的旧请求诊断身份当成本次验证。

Webview 继续先取得对应缓存及其 ETag，再请求宿主校验；宿主返回 NotModified 时复用缓存，返回新字节时沿原数据流显示及更新缓存。必须先核验原 Webview 根限制及当前 owner 权限，不能凭已存在缓存绕过鉴权。Webview 自己生成的受控响应缓存与 raw HTTP 响应是两个既有层次；允许原链路自然命中，不能据此追加第三份应用自管字节库、双写清单或原生下载目录。没有有效 validator 的资源沿原有界读取，不伪造固定版本；上游 stat ETag 的修改时间／大小观察值只用于再验证，不能冒充强内容 hash、完整版本快照或文件保存的 CAS 基线。不为生成缓存 validator 在每次 stat 全量读文件；媒体缓存或其内部 version 参数不推进保存基线。

稳定 Gateway／工作区／规范资源归属沿唯一 URI 投影传入原资源地址；origin／profile 与当前认证上下文共同限定缓存使用。route 改变时重解原 owner，route 被另一个目标复用不得沿旧身份命中。退出／切换身份取消旧读取并撤销旧 Webview／object URL 的展示引用；新身份在校验成功前不消费旧资源。原 watcher 变更、删除、重连或 overflow 通知沿 FileService 刷新；刷新重新校验，显式强制刷新可沿标准 reload 绕过旧字节重取，不以旧校验结果掩盖通知。没有强版本且修改时间／大小均未变化时，不承诺发现绕过 owner 的任意改写；不能以此弱观察值证明可复现版本。

Web 由浏览器管理对应 origin 的 HTTP 缓存与 Cache Storage；Electron 沿既有 `persist:boxteam`、稳定 renderer origin 及 `${BOXTEAM_HOME:-~/.boxteams}/clients/electron` 的 profile 使用相同机制，由 Chromium 管理磁盘格式。正常重启不清这些资源缓存；不增加 native FS IPC、手工 home 文件副本或另一份 renderer 源码。容量与淘汰沿引擎配额及上游缓存命名／版本生命周期，不新增用户必填配置或无界资源预热；缓存是可丢弃优化，清理仅影响所属资源缓存，不清聊天历史、设置、草稿或后端文件。官方隔离 Webview Worker 的 `cache.put` 沿用上游尽力写入：其配额／写入异常不影响已取得资源的响应，不新增日志、宿主上报或写入探测，也不为此复制 Worker 或接管隔离宿主部署；成功读取不表示缓存已持久化。这是本轮采用的行为，不列为待实现诊断或上游待办；其它缓存层继续保留原诊断要求。目标断开、删除或无权限时明确读取失败，不默认用旧图片冒充当前成功，不新增离线优先模式。

验收必须分别覆盖真实 Web 和打包 Electron 的普通图片编辑器与 Webview 文件资源：首次有真实字节，第二次及正常重启后校验未修改且不重复传正文，修改后新字节及新 validator，A/B 同路径、认证切换与 route 重用无串读，删除／断连不伪报成功，Range 不误写完整缓存。记录条件请求、服务端实际状态和正文传输量；浏览器可能把网络 304 与缓存合成为 fetch 200，不能只断言 JavaScript 响应码，也不能只看图片可见或缓存目录存在。缓存淘汰后按同一读取链重建，官方 Webview Worker 的缓存写入沿用上述上游行为；原文字保存／revision、未保存模型与图片视图操作定向回归，每次验收绑定同一源码及构建指纹。Webview 宿主和 Worker 必须匹配固定上游，现有隔离宿主未就绪的失败不得算通过，也不能靠替换原编辑器绕开。

## Validation

按任务各自的源码依赖复用准确证据；发生变化后补受影响检查。同一原 owner 的纵向链迁齐后集中验证，不默认重复全套构建或跨层长回归。真实 Web 与打包 Electron 证据分别登记，全部执行带进程外超时、内存保护及隔离产物路径。

## Migration Plan

没有因文档拆分产生的数据迁移。原数据、缓存、profile、草稿、备份和历史证据保持；实际变更格式时由原 owner 提供明确迁移，不能靠清空存储恢复。
