本清单承接快速访问原第 17／18 组，沿用编号以对应原收据。已完成项只表示原有通过范围；未完成项不因拆分而通过。本变更独立验收，不阻塞 `add-workbench-quick-access` 归档。旧收据位于该变更 tasks.md 的具名历史记录；后续按依赖复用，不复制或改写原结果。

## 17. 资源字节持久缓存（2026-11-03 补充设计）

覆盖规范：原文件预览复用私有条件缓存；Webview 资源缓存沿原版加载与隔离规则；资源缓存跨 Web 与 Electron 持久且可丢弃。

本组按实际实现和验收证据逐项勾选；此前图片解码、Range、文字保存及 Electron 历史缓存证据不能替代资源字节缓存证明。复用第 2／4 组的唯一文件打开与读取链，按完整纵向链迁齐原消费者和验收，不设定全局实施顺序。

- [x] 17.1 清点普通图片 FileService／Blob、Webview loadLocalResource／Worker、raw 读取、附件预览、Gateway 与 Electron 回环代理的实际调用方和相关测试；确认同一原文件入口与 URI 投影，明确第三方 URL、下载／临时数据／Range 边界，不复制图片编辑器、Worker 或资源解析器。
- [x] 17.2 在原文件 owner 为普通预览完整 raw GET 实现 private/no-cache、有效 ETag 及认证／原目标校验之后的空正文 304；Vary 合并实际工作区／后端身份与认证头／Cookie，validator 绑定稳定资源及版本观察值。保留下载／临时／Range／失败的 no-store，304 不完整读正文、不沿旧 request_id，mtime／size 不代替内容 hash 或保存基线；同步相关 API 描述及真实类型消费者，移除该预览场景旧的统一 no-store 分支。
- [x] 17.3 沿标准 fetch HTTP 缓存接通原图片编辑器，保持原 FileService、Blob／object URL 生命周期；贯通 Gateway／回环代理条件头、缓存头、Vary、304。原 Webview 复用固定版本 Worker 和宿主 ETag／NotModified 链，不自建字节缓存服务或文件落盘 IPC，不通过替换原 Pane 绕过宿主条件。
- [ ] 17.4 对齐稳定 owner／URI、HTTP Vary、Webview 根限制和认证切换；验证 watcher 更新／删除／重连／overflow、强制刷新、route 改变／重用及取消后迟到响应。覆盖缓存存在时的权限拒绝／断连，不能使用旧字节伪报当前成功，媒体内部 version 不成为业务版本。
- [ ] 17.5 复用 Web origin 与 Electron 原固定 profile／partition／renderer origin 的持久存储，正常重启不清缓存；处理引擎淘汰、配额及写入诊断，资源清理不删设置／聊天／草稿／业务文件，无全量预热、home 镜像、另一份前端或新增原生能力。
- [ ] 17.6 以隔离 A/B 同路径真实图片、UTF-8 文本及原 Webview 允许根中的 CSS／字体／脚本，经 Gateway 分别验首次／二次／更新／删除／断连／认证切换／route 重用、Range 和淘汰；按原读取 owner 保留 MIME、条件请求、服务端真实 200／304／206、正文传输量及缓存失效证据。不得只断言 fetch 状态码或可见图片；定向回归文本保存 CAS、未保存模型、备份和原媒体操作，不默认全套重跑。
- [ ] 17.7 对同一源码及正式构建完成相关静态分析、真实类型检查和 Web build，并用可迁移打包 Electron 产物真实验普通图片／Webview 的正常退出重启及零正文重传；无开发服务器或源码目录依赖。所有执行性检查带进程外超时／内存保护，正式产物按测试路径保存；上游 Worker／宿主或原生条件缺失明确未通过，不用 Web 证明替代 Electron。

2026-11-05，17.3 原 Markdown Webview 纵向接通：允许根内 HTTP 扩展资源沿原 asWebviewUri／官方 v6 隔离 Worker／FileService 加载，根外 URL 保持原语义；首次渲染前发布同一允许根，HTTP Provider 的占位 stat 不充当 validator，业务文件保留原 ETag／NotModified，原字体 MIME 补齐。真实 Gateway／正式 Workbench／Chromium `1 passed / 72.70s / exit 0`，首次 CSS／字体 raw 正文分别 130／154012 字节，热刷新仍 describe 且二者 raw 请求为零，修改 CSS 后读取新正文并显示新颜色；math／mermaid 贡献资源首次经 Worker 成功，扩展资源失败为零。无响应替身、无模型调用，pageErrors 为空，保留两条原生关闭长操作诊断。正式构建 `exit 0 / 38.11s`，49 份转换源码的完整宿主类型检查 `exit 0 / 50.83s`；转换／扩展复制 10 用例、回环代理 5 用例以及 Ruff／Python 类型检查通过。回环测试使用明确 HTTP 替身，仅证明条件头、owner／认证、Vary／ETag、空正文304及新 request_id 透传。收据与准确指纹：`out/tests/temp/2026/11/05/webview_resource_cache/artifacts/verification-001.json`（SHA256 `480b55c74aaaa98d282ff25696ba59dc8cf978b8ebfbe28508cbba1c0c1cca18`）。当前图片回归在 conftest 导入阶段被其它会话尚未落位的 `session_catalog_store/migrations/nodes.sql` 挡住，未进入产品场景；原件保留，未重复无变化失败。17.3 整项及 Electron 重启、认证切换／route、淘汰和其余17组场景继续保持待验收，不由本片替代。

2026-11-05，17.3 补齐原图片当前运行时回归：完整提交 `12013f1f1` 与固定上游均实际 archive 解包，真实 Gateway／A、B 托管后端／正式 Workbench／Chromium `1 passed / 94.33s / exit 0`，5502个快照文件前后不变。首次原图69字节、热读真实304／零正文、另一owner同路径独立200，修改／watcher、权限失败／恢复、删除／重建、UTF-8文字304、CAS、Range206及下载／错误no-store均通过；文本分屏并发只读一次、共享dirty正文、独立选区、关闭一组保留编辑、外部冲突及最后关闭后重读也保持，pageErrors为空。正式构建 `exit 0 / 41.22s`。11份直接运行时／驱动源码与共享树相同，复用前述原Webview和回环代理证据，17.3现已闭环；收据 `out/tests/temp/2026/11/05/cache_committed_product/artifacts/verification-002.json`（SHA256 `6695c52dafa130c70efe4d9bb0f00739a5bc0c83e3982ad24776b041a7f5ffee`）。隔离首轮误用上游符号链接，导致esbuild真实路径越出快照、转换未命中；改为完整上游archive后通过，未改产品或测试断言。原始输入失败均保留，不由本项证明其它会话未完成的SQL／配置／装具改动，也不替代17.4～17.7、18.2／18.10或打包Electron验收。

2026-11-05，17.1 完成实际入口清点：原图片与 React Markdown 图片共用普通 raw，原 Webview 沿隔离 v6 Worker／FileService，附件保留 Session owner；第三方 URL、Range、下载／临时及失败边界独立。路径、源码指纹及证据归属见 `out/tests/temp/2026/11/05/cache_cancellation/artifacts/inventory-001.json`。17.4 修复原 Provider 在路由解析／字节转换取消后仍交付迟到结果的两个真实反例；同时迁齐旧 watcher 装具的稳定 owner、资源 URI 与具名导航入口，保持首次建连零伪更新。定向12用例／77断言、49份转换源码完整类型检查及正式Web构建通过，准确命令、前后失败原件及指纹见 `out/tests/temp/2026/11/05/cache_cancellation/artifacts/verification-001.json`。此片仅证明具名 Provider／传输边界，17.4 的身份切换、断连等及 Electron 仍待验收。

2026-11-05，17.5／17.6 补齐 Webview 引擎淘汰场景：完整 `76649e377`／固定上游快照、正式 Workbench 和真实 Gateway `1 passed / 74.83s / exit 0`，4406份源码前后不变，正式构建 `exit 0 / 39.28s`。只通过原 Cache API 删除 CSS／字体两条记录，原 Worker／宿主重新读取并建立130／154012字节资源；另一个真实加载的52字节 CSS 条目保持原 ETag／MIME／正文长度且零新增 raw。无响应替身、无模型调用、pageErrors为空，原生关闭诊断保留；Ruff、Python类型及驱动ESM检查通过。首次空保留集结果另存，不用于证明非目标记录保留。收据 `out/tests/temp/2026/11/05/webview_eviction/artifacts/verification-003.json`（SHA256 `5aeec2fb6a94d3ffabcaf1bb045fd93550d0f6776a6dcf046315d1a12a8b098c`）；该片不代表产品新增清理入口，配额／写入诊断、身份／route、受保护数据和打包Electron仍待验收，17.5／17.6保持未勾。

## 18. 分层缓存接入与恢复数据保护（2026-11-03 扩展设计）

覆盖规范：缓存必须按原服务分层管理；编辑器模型共享必须保持恢复与版本归属；派生缓存必须绑定适用身份与代际；缓存清理与升级不得删除受保护恢复数据；运行资源与代码缓存必须遵守实际构建和宿主边界。

本组扩展分层合同和待验收范围，不表示新实现已完成；快速访问原第 16 组负责查询缓存，第 17 组负责资源字节，本组复用其证据而不重复建缓存或复制整套测试。字段、条目数量和宿主内部磁盘格式不在本稿预造；各项按真实服务适用性及原 owner 纵向闭环推进，不要求新建统一缓存管理服务。

- [ ] 18.1 按 design 分层矩阵核实际缓存及全部直接／间接消费者、key／版本、存储、失效／释放和容量／淘汰；明确 metadata 仅观察值、条件服务未启用及纯内存缓存。落实唯一 owner、必要 scope 与代际，保留无缓存时原读取；不创建 CacheManager、全局索引、统一字节库或运行时注册平台。
- [ ] 18.2 沿原 TextFileEditorModelManager／resolver 迁齐文件、只读来源、Diff／历史输入与 editor consumers；验证同资源并发 resolve、分屏正文共享／视图独立、关闭一组不影响另一组、最后引用释放、dirty 遇后端更新不丢编辑及不同 owner／revision 隔离。无需为干净正文新增持久模型镜像。
- [x] 18.3 核原文件 Provider stat／describe、导航缓存及 watcher 实际语义，已有缓存按完整资源身份在更新／删除／重连／overflow 后失效；没有则保留真实轻量请求。验证不永久缓存 not-found／权限失败、不因校验推进保存 revision，复用第 17 组字节合同而非再造 stat 缓存。
- [x] 18.4 接通实际 ConfigurationCache 的 needsCaching／type-key／队列与原 profile／资源归属，保持本地 workbench 设置不重复缓存、非本地配置按原链更新；验证修改后刷新／正常关闭竞态、范围／身份变化及迟到写入，不改变后端业务配置 owner 或以清空数据修复错误。
- [ ] 18.5 核模型 tokenization、实际语言 Worker／索引、Web 扩展扫描／元数据缓存的真实入口与适用条件；按模型／语言／配置／扩展版本失效和原生命周期释放，验证迟到分析不能覆盖新模型、业务 URI 和范围有效。无真实服务列明未启用，不因 Electron 引入 native CachedData／scanner、扩展宿主、LSP 或全局符号索引。
- [ ] 18.6 按原 owner 核查询缓存、已加载导航页、Gateway 目录镜像、聊天历史镜像和领域索引的用途隔离，复用快速访问原第 16 组受限预热／cursor 合同；验证 revision／身份改变、明确 stale 及原历史镜像先显后同步，不把文件读取失败的规则误用于删除既有只读历史预览，不扫描私有目录重建。
- [x] 18.7 在唯一构建／server factory 及原静态资源消费者落实内容地址长期复用、未 hash main／Worker／入口再验证和升级一致性；保持 OAuth no-store、Vite/HMR 及原 Webview 隔离版本。验证旧缓存存在时新 bundle／Worker 实际生效、缺资源失败，Chromium 代码缓存只记录真实宿主边界而不导入 native bootstrap。
- [ ] 18.8 复用原命令面板／设置／输出和 owner 清理方法，按具名可再生类别与明确作用域释放，补实际命中／再验证／stale／未命中／绕过／存储失败诊断；清理不删除备份／本地历史／草稿／设置／恢复数据／信任／凭据，无选择性 API 时沿 reload／淘汰，不加 bridge、清站点数据按钮或遥测后台。
- [ ] 18.9 沿原 browser WorkingCopyBackup／History、Composer 及原展示态存储核正常关闭、既有恢复能力、升级和格式失配；可再生缓存可以失效，受保护数据保持原件并明确错误。通过实际未保存正文、草稿、分屏／光标／滚动、设置、最近记录证明清缓存／重启未丢数据，不对原服务未承诺的异常退出扩大恢复保证。
- [ ] 18.10 在隔离工作区按层选有判别力的冷／热态、失效、取消、释放、清理与升级场景，通过真实 Gateway 的 Web 和可迁移打包 Electron 分别验收；同时有多个业务工作区与模型，核实际网络、引用／模型身份和保留数据，而非只凭耗时。复用快速访问原第 13～16 组和本变更第 17 组的同源码已通过证据，按变化依赖补受保护静态／真实类型／构建和定向检查，不默认全套重跑；保存源码指纹、真实产物、命令、原始结果及未验证条件。

2026-11-05，18.4 当前 Web 运行面验收：`test_configuration_cache.py` 原版正式 Workbench 与独占资产副本精确 IDB 写屏障两变体 `2 passed / 220.74s / exit 0`；真实 Profile、A/B 非本地配置、迟到写 21→22 及写 23 未放行正常关闭重开均通过，未清空存储且模型请求为零。正式构建 `exit 0 / 38.82s`，同源 ConfigurationCache／FolderConfiguration 的 7 用例与历史连续保存刷新按指纹复用。完整命令、源码／资产指纹、原始结果及边界见 `out/tests/temp/2026/11/05/configuration_cache_delivery/artifacts/verification-002.json`（SHA256 `4a07b239659772dedd85318a32e4ac5ca14ee66ef290b08d2628fef7f04cb888`）。两节点 pageErrors／diagnosticErrors 为空；console 511／754 条包含尚未逐一归因的 404／fetch 错误，不能据此声明产品零错误。18.10、Electron、其它缓存层及当前全树类型／构建不由本收据代替；插桩仅限独占资产副本，另保留未插桩对照。

2026-11-05，18.7 补齐开发源码更新并核对现有证据：独占完整源码／上游快照、产品 Node／Vite／Gateway／Chromium `1 passed / 150.02s / exit 0`，真实修改导航观测属性后收到 Vite update／full-reload，页面自动显示新值；未手动 reload、无响应替身、三初始化 API 的稳定目标与 request_id 正确，零模型调用。Ruff、Python 3.12 Pyright 和驱动 ESM 检查通过。原 Gateway／共用静态 server 指纹与 11/03 收据一致，构建及 Web 静态升级驱动与 11/04 收据一致，复用其真实内容地址分块零正文、main／editor Worker 304、新版执行、缺 Worker 404 和 Linux 正式 ASAR 证据；不将后来改变的 Electron 测试或整树视为同一源码。Webview 已由 MCP 入口变更切至官方 v6 隔离资产，复用固定上游／发布 Worker 的绑定归一等价与真实原详情页面证据，不重做旧 v4 失败。完整命令、指纹、原始失败与通过记录及依赖判断见 `out/tests/temp/2026/11/05/workbench_hmr/artifacts/verification-001.json`。HMR 记录保留副本依赖链接导致的两次字体 403 及原生关闭诊断，pageErrors 为空，不宣称控制台零错误；Webview 文件缓存、其它平台与引擎内部缓存收益仍不由本项证明，17 组和18.10保持原状态。
