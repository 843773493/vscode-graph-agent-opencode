本清单追踪实现与验收进度；已获授权按纵向切片推进，完成项须附实际证据。章节编号用于定位，不代表排期；领域合同、真实查询和客户端消费的必要依赖须满足，可独立的任务不要求串行。每个实现项完成时须同时迁齐直接／间接调用方及相关测试，删除失去消费者的同义旧链路。不得把文档齐备、入口可打开或单项辅助函数通过当作产品完成。

## 1. 合同与真实类型

覆盖规范：搜索结果必须明确类别归属与目标；索引必须由各搜索 owner 自主负责。

- [ ] 1.1 按 design 的具名 owner 对照现行 schema、查询入口与所有直接／间接消费者，确认目录使用 SQLite catalog、Thread 使用精确 SessionThread，不沿用物理目录重建条文。
- [ ] 1.2 为领域搜索新增的请求、命中、独立位置和分支状态补齐原 owner 的 typed 合同；涉及公开 API 时同步其 delta、OpenAPI 与生成客户端，不创建全局搜索 DTO 或第二客户端。
- [ ] 1.3 为文件、目录、消息、工具、请求和日志分别确定必要的查询预算、上限和覆盖说明，复用原 owner 配置／常量；索引技术各自决定，不强建全局索引。
- [ ] 1.4 完成共享签名和生成类型的真实检查，核真实序列化结果、Unicode 位置单位和错误归属，阻止只改 TS 声明但后端不提供数据的接入。

## 2. 稳定目标与统一打开

覆盖规范：规范 URI 必须按资源引用与重开需求补齐；URI 解析与定位参数必须消费唯一合同；打开动作必须保留原版编辑器行为和入口差异。

- [x] 2.1 将 ThreadInput 的匹配、去重和恢复绑定稳定 Gateway／workspace／Session／Thread 身份，重开时重新解析已登记 route；失效或歧义不改投当前目标。
- [ ] 2.2 在原 opener／resolver 接入规范资源 URI 的唯一解析与编辑器投影，覆盖精确 child、文本、媒体、Diff 和只读资源；位置和版本使用独立参数。
- [ ] 2.3 清点需要复制引用或重开的对象，已有规范 URI 直接复用，确有模板缺口时由唯一寻址 owner 登记并迁齐消费者；命令与瞬时状态不造 URI。
- [ ] 2.4 验证原编辑器组、预览／固定、侧边／后台打开、目标组及视图恢复，文件树单击仍在会话资源内预览，多种打开方式由明确动作触发。
- [ ] 2.5 用同名跨工作区、route 改变／被复用、main／child、只读与不可文本资源的真实打开入口验证目标隔离，不能只测投影函数。

## 3. 目录与 Thread 发现

覆盖规范：项目目标和既有业务动作必须逐类接入。

- [x] 3.1 在原 catalog owner 补齐名称／ID／目录字段的模糊召回，保留查询与 revision 绑定的分页，folder 仅作为导航目标。
- [x] 3.2 在原 Thread owner 补工作区范围内 main 与未打开 durable child 的有界发现，结果携带精确归属，不拉全部 Session 后在前端遍历。
- [x] 3.3 将 Session 候选交由 owner 解析不可变 main_thread_id，Thread 候选保持 child 身份，folder 默认动作定位原导航。
- [ ] 3.4 验证首批之外的间隔字符缩写、同名 Session／main／child、目录 revision 变化与删除命中，区分未命中、受限及失效 cursor。

## 4. 文件 owner 与真实目录根

覆盖规范：文件搜索与文本定位必须共享实际资源目标。

- [x] 4.1 改透唯一文件内部投影，使规范目录根支持真实 stat／describe、joinPath、相对路径及往返定位；旧叶子 query 投影的全部消费者一起迁移，不新增搜索 scheme。
- [x] 4.2 在原文件搜索 owner 接入有界模糊文件名／路径查询，保留 cancel、cursor、扫描／字节／时间限制和截断说明。
- [x] 4.3 补齐原界面已展示的正则、大小写／smart case、整词、include／exclude、ignore、仅已打开和结果上限语义；尚不支持的选项明确拒绝，不静默按 substring 执行。
- [x] 4.4 验证真实目录不存在、只读、非文本、Unicode／多行位置和查询选项；保留文件保存的 revision／CAS 冲突合同，不引入新的批量替换业务。

## 5. 原版文件搜索服务

覆盖规范：文件搜索与文本定位必须共享实际资源目标；搜索范围必须明确且请求保持原目标。

- [x] 5.1 通过原 singleton 注册唯一项目 SearchService 和同一文件 file／text Provider，核最终 ServiceCollection 与生命周期，不依赖偶然 import 顺序。
- [x] 5.2 对明确业务根的 textSearchSplitSyncAsync 使用完整 owner／URI 过滤本地未保存模型，先限制再发送 progress，当前模型覆盖同文件后端旧命中，不自动保存。
- [x] 5.3 将业务查询显式路由到原后端文件 owner，不送浏览器文件 handle worker；原生虚拟容器仍按原服务行为处理。
- [x] 5.4 在原 `%` 贡献构造入口用 child InstantiationService 创建真实原 Provider 与 QueryBuilder，每次 provide 固定发起范围，取消和 dispose 释放实际操作。
- [x] 5.5 限定 folderQueries、extraFileResources 与符号来源；业务 AI／语义搜索缺服务时明确未启用，不触发模型或其它根，不改全局设置。
- [x] 5.6 用 A／B 同路径不同 dirty 内容及 IndexedDB 同名文件验证首次结果、增量 progress、普通／仅已打开搜索均不串范围。

## 6. 原搜索视图与 Search Editor

覆盖规范：搜索编辑器必须按输入持久保存明确范围。

- [x] 6.1 在原 SearchView descriptor 选择项目子类，并在原 ViewPaneContainer.createView 以 child 创建；移动到其它容器仍保持唯一视图 ID、用户布局、全局 SearchViewModel 和 ReplaceService。
- [x] 6.2 将 `%` 到 SearchView 的 replaceSearchModel 与 SearchView 到 Search Editor 的两个 rawData 分支携带同一明确范围，后续修改查询不跟随导航。
- [x] 6.3 在原 SearchConfiguration 增加 typed、版本化 scope，贯穿 factory 三种入口、控件重建、updateConfig 和命令参数；priorConfig 只复用查询选项，不继承 scope。
- [x] 6.4 在同一 sourceTransform 的原 SearchEditor 查询构建处接入 protected typed hook，实际使用 input scope、child QueryBuilder、根及受限 extras；不改私有 readonly 字段，不吞构建错误。
- [x] 6.5 在原 Pane 贡献选择 SearchEditor 子类，setInput／clearInput、模型 await、原 searchModel 取消及异步完成均检查 input／scope 代际，复用仅匹配同 scope。
- [x] 6.6 让原 JSON serializer、copy、磁盘／backup header 读写保留 scope，修复固定六行 header 读取；缺 scope 的旧结果只读查看至明确选范围，未知／损坏版本明确报错。
- [x] 6.7 用两个工作区同时分屏验证再次查询、复制、保存／另存、关闭重开、backup 恢复和 route 变更，检查 query 对象与真实 HTTP 目标；不以保存字段存在替代运行时接线证明。

## 7. 默认 Provider 与模式帮助

覆盖规范：默认入口必须统一组织资源并明确工作区范围；输入必须直接可用并解释当前模式和匹配；列表必须分层展示并保持动作可辨认；原入口调用方式必须保持兼容语义而不增加重复入口。

- [x] 7.1 复用既有 sourceTransform 在原贡献登记处选择唯一空前缀及 `%` descriptor，保留原元数据；固定 commit、锚点恰好一次且失配失败，dev／预构建／build 使用同一管线。
- [x] 7.2 基于原 PickerQuickAccessProvider 接入原 fuzzy scorer、历史和打开服务，非空按最近／Session／Thread／文件分组，组内排序与稳定去重符合配置。
- [x] 7.3 在 Quick Input 聚焦前捕获发起上下文，空输入历史只限已选范围，名称／描述／详情、图标、高亮和行内动作准确，不枚举全部资源填空。
- [ ] 7.4 保留 URI 粘贴、文件追加行列／符号、quickNavigate 和原 filesOnly／fileSearchDelegate、filter／additionPicks、handleAccept、按钮与 prefix 限制的真实调用语义。
- [x] 7.5 接入 `?` 与 Command Center includeHelp 的原注册帮助、命令及选模式转接，保留发起范围与接收者，默认模式关闭递归帮助。
- [ ] 7.6 验证空文件编辑器的 delegate／cache 生命周期与原 Chat 二级 picker 取消返回，不将原 widget 接受动作转投项目 Composer。

## 8. 查询范围与异步结果

覆盖规范：搜索语义和异步边界必须真实；加载必须渐进反馈并避免更新误选；每个查询分支必须独立表达完成与受限状态；续页必须绑定原查询并保持选择目标。

- [x] 8.1 用原 Quick Input 提供已登记范围集合选择，明确显示当前范围；取消保持原查询，确认才发起新范围查询，选择停止目标不自动连接／启动／激活。
- [x] 8.2 单目标经 Gateway 显式路由，多目标对所选 owner 有界并发；若使用原 Gateway catalog 聚合，先补其显式目标过滤，不调用全范围后隐藏 C。
- [x] 8.3 按查询／范围／类别／身份代际取消排队和请求、废弃迟到响应；服务端继续工作时仍受原预算和总 deadline 约束。
- [x] 8.4 为每个工作区／owner 展示 busy、完成、受限、失败、取消和新鲜度，快结果先出，重试只针对失败读分支，全部失败不伪报无匹配。
- [x] 8.5 使用每个 owner 独立 cursor 和明确续查，绑定原查询及 revision；合并保持有效活动目标和滚动，失效时不把 Enter 应用到新占行对象。
- [x] 8.6 记录实际请求证明只 A 或明确 A+B，不访问 C；覆盖取消、超时、离线、旧 cursor、重复输入与迟到 progress，不仅查看 UI 结果。

## 9. 五类正文查询与精确定位

覆盖规范：内容搜索必须覆盖明确选择的正文类别；正文命中必须能够加载并精确恢复位置。

- [ ] 9.1 接入 `content ` 的类别选择与按 owner 分支组织，默认 Ctrl+P 不扫描正文；仅选类别发请求，`%` 固定文件并复用同一文件条件。
- [ ] 9.2 文件类复用第 4／5 组的唯一搜索链，提供有界片段和独立文本位置，不另建正文文件扫描器。
- [ ] 9.3 在原 canonical history owner 实现 main／child 消息正文有界查询和按命中加载，未打开及未加载页可定位，不查询前端缓存冒充完整历史。
- [ ] 9.4 在原工具结果 owner 实现有界正文查询、执行／结果身份与精确读取，复用原工具详情，查询不触发工具重放。
- [ ] 9.5 在原 LLM 请求记录 owner 实现有界正文查询及请求详情定位，不拉全量日志到前端或把它重复作为聊天命中。
- [ ] 9.6 在原 Trace／运行日志 owner 实现分类正文查询及记录定位，区分工作区与 Gateway 日志，不越过明确范围与权限。
- [ ] 9.7 将命中通过精确 owner、目标组与请求代际交给 React 虚拟列表／原详情加载，等待缺页与实际挂载后定位，失效记录明确报错而不回顶部。
- [ ] 9.8 用各类独有词、未打开 child、未加载工具／日志、多行与 emoji、截断片段及已删除记录验证真实 owner 查询和位置，未选类别没有请求。

## 10. Thread 大纲与原版导航

覆盖规范：Thread 大纲必须复用非文本编辑器导航语义；项目编辑器和视图必须进入原版导航机制。

- [ ] 10.1 为 Thread 注册原 Outline creator，使用 quickPickDataSource、preview／reveal／captureViewState；披露历史覆盖并补取缺页，取消只恢复对应组展示态。
- [ ] 10.2 保留文本 `:`／`::`／`@`／`@:` 语义；`#` 及 includeSymbols 仅接实际可限定根的符号服务，未具备时明确未启用，不伪造符号或查全局后过滤。
- [ ] 10.3 核对 `edt active `／`edt `／`edt mru ` 中 Thread／文件／Diff 的名称、类型、组、dirty 与关闭语义；同目标多组副本仍可准确选择。
- [ ] 10.4 让 `view ` 发现并聚焦已有会话导航与工作区面板，保留原视图位置和归属；验证文本与 Thread 切换、大纲预览取消和未挂载历史。

## 11. 领域动作与资源模式

覆盖规范：快速访问必须区分目标选择与动作执行；候选展示不得触发业务执行；管理动作必须复用领域权威状态；跨入口命令必须保持目标优先级与原打开修饰键。

- [ ] 11.1 将项目业务动作注册为同一 Action／Command，迁齐原菜单、React 操作和 picker 消费者；目标优先显式参数，再发起 editor owner，仍不明确才选择。
- [ ] 11.2 接入 `session `／`thread `／`resource `，覆盖 Skill／AGENTS／配置、附件／工具结果、变更的真实数据与能力，非文本交相应视图，不为各类再建通用状态机。
- [ ] 11.3 接入 `automation ` 的任务详情、已有开始／暂停／继续／编辑／终止／删除／试运行及历史操作，按原状态和 revision 可用；暂停任务不冒充停止 turn，试运行不自动执行后续动作。
- [ ] 11.4 接入 `workspace ` 的真实控制面候选、范围选择与独立切换／连接／启动动作，执行时核验冻结目标，不因浏览候选启动后台服务。
- [ ] 11.5 接入终端／浏览器／MCP 等已有资源的查找、聚焦和原生命周期动作，保持 stop／detach／delete 区分及合法关联范围。
- [ ] 11.6 统一 pending／confirmed、完整权威替换、失败重新读取与未知结果对账，取消未接受步骤不写入，关闭已受理浮层不撤销／重放后端动作。
- [ ] 11.7 验证未打开详情直接管理、显式 child 优先、任务 revision 冲突、目录 outbox 回执和预览无写副作用，移除迁齐后无消费者的同义执行链。

## 12. 引用与 Composer

覆盖规范：引用动作必须固定来源与接收者；文件引用粘贴内容和可复现引用必须分别处理；选择和参数输入必须复用成熟交互并保持无副作用。

- [ ] 12.1 将复制 URI、引用、粘贴和明确固定版本作为独立动作，使用原只选择／多步 Quick Input，不抢占打开的侧边／固定／后台修饰键。
- [ ] 12.2 在发起时固定来源及接收 Thread，全局入口无接收者才选择，切换焦点／范围不改投；接收者失效明确失败，不猜最近 Thread。
- [ ] 12.3 将支持的原资源引用交给精确 Composer 草稿，附件仍经原上传 owner，来源预览不激活，加入草稿不发消息、不启动 turn。
- [ ] 12.4 粘贴保留选定正文，显式可复现引用由原 owner 核验版本；无法取得版本明确失败，不新增统一快照／冻结体系。
- [ ] 12.5 验证多 Thread 同时可见、跨工作区来源、二级返回、多选取消、不支持能力和来源变化，只有指定草稿变化且无隐式模型调用。

## 13. 展示态与恢复

覆盖规范：历史和缓存必须保持展示用途；展示态迁移必须保持单一恢复链与身份隔离。

- [ ] 13.1 复用原资源／命令历史及输入记忆配置，按身份隔离；从历史移除、关闭 Tab 和删除业务资源相互独立，不持久化 token／临时 endpoint。
- [ ] 13.2 为实际变化的编辑器／历史格式实施一次显式版本迁移，保留可核验身份、每组视图及既有草稿／working-copy 恢复，之后只写新格式。
- [ ] 13.3 对缺失身份、已删资源和损坏／未知版本提供明确反馈和展示记录移除，不清空全部存储、不扫描磁盘重建或猜同名目标。
- [ ] 13.4 验证 Web 浏览器存储与 Electron 固定 profile 中重启、route 更换、Gateway／凭据变化和不可恢复旧记录，旧候选及动作立即失效，不新增 IPC。

## 14. 交互与宿主条件

覆盖规范：键盘确认取消与返回必须沿用原版语义；焦点转移失焦关闭和定位预览必须遵守原版行为；原版快速访问操作习惯必须保留；原版服务与项目同名能力必须区分实际接入条件。

- [ ] 14.1 按 design 六类 22 项交互基准验证自动焦点、提示、高亮、分层列表、行内 Tooltip、上下／翻页／首尾、Enter／修饰键、Esc 与返回。
- [ ] 14.2 验证支持 Provider 的后台连续接受、失焦设置、取消定位预览恢复、IME／ARIA、主题、输入记忆及活动项／滚动保持，浏览器拦截键保留可用命令。
- [ ] 14.3 按全功能矩阵核原生 term／task／debug／debug consoles／mcpr 的实际服务与项目 owner 映射；原服务缺条件不伪报互通，项目核心入口仍真实可用。
- [ ] 14.4 核 ext／ext install／issue 的实际宿主及发行配置条件，记录可用或明确缺失原因，不为快速访问新增原生扩展体系或外部发行服务。

## 15. 产品验收与交付证据

覆盖规范：接入完成必须具备真实入口与运行条件证据。

- [ ] 15.1 按测试矩阵登记对应定向协议／owner／客户端检查及隔离 fixture，使用进程外超时、内存保护和镜像测试产物路径；独立 fixture 可并行，失败先归因，不默认串行全跑。
- [ ] 15.2 完成相关后端静态检查、真实前端类型检查和 `bun run --cwd src/clients/web build`；同一 sourceTransform 生成贡献必须纳入类型证明，复用同源码已完成构建，不重复无变化检查。
- [ ] 15.3 通过实际 Gateway、目标后端和 Workbench 浏览器入口核初始化三个 API 与 request_id，执行 Ctrl+P、Command Center、URI、引用和管理操作，保留请求范围及错误原件。
- [ ] 15.4 在真实 Electron 打包产物核同一入口、快捷键、会话操作和重启缓存／范围恢复，不用开发服务器或浏览器 parity 替代桌面证明。
- [ ] 15.5 为规范全部场景和 design 全功能矩阵逐项关联准确证据，区分通过、未通过及真实条件缺失；保留源码指纹、命令、原始输出、退出码和未完成阶段。
- [ ] 15.6 交付前核无重复默认 Provider、第二搜索／动作事实源、失联调用、静默错误或清缓存式迁移，保留其他人的改动；复核 OpenSpec 并仅按已证行为勾选任务。

### 2026-11-02：Session／Thread 专用模式（11.2 部分）

- `session ` 和 `thread ` 通过原 Registry、帮助 metadata 与 Action 接入同一 Provider；分别只查询 catalog 或精确 Thread owner，范围、分页、失败、取消和打开继续使用唯一链路。Session 名称不按文件行号／符号拆分；Thread 最近记录不混入文件。
- 真实 Gateway／正式 Workbench 验收从两个原命令打开选择器，确认 Session 打开原 main、Thread 保留 child，实际只出现一次 catalog 查询和一次 Thread 查询，均限定 A，没有额外文件查询、业务执行或页面错误：`out/tests/e2e/clients/web/test_quick_access_catalog_targets/runs/run-4e1b4ef34a25/nodes/node-11ce5d55e445/artifacts/catalog-targets.json`；1 passed / 121.91 秒。该运行同时保留原 URI、分页和目录失效回归。
- 9 项 Provider 合同、真实类型检查（含 18 份转换源码）、正式构建通过；原输出与退出码以 `out/tests/temp/quick_access_opener_20261102/artifacts/name-modes-*` 保留。`resource ` 的其余类别尚未接入，11.2 保持未勾选。

### 2026-11-02：规范 URI 打开切片（2.2、7.4 部分）

- 认证 Gateway 的只读地址投影复用唯一 URI grammar，公开 DTO 从 proto 生成并递归检查。原 Opener 重新解析稳定工作区路由；Session 沿原 owner 取得 main，child 保留精确 Thread，文本文件复用唯一文件投影。原 URI 不先按行号或符号改写；目标不可用和非法 URI 明确失败。发起组、侧边、固定与后台选项保留，校验成功前不创建侧边组。
- `tests/e2e/clients/web/test_quick_access_catalog_targets.py` 经正式构建、真实 Gateway 和产品创建／fork 入口验证 child、Session main、侧边组、跨工作区同名文本及非法 fragment；最终 1 passed / 121.23 秒，证据：`out/tests/e2e/clients/web/test_quick_access_catalog_targets/runs/run-444f14cfbcd7/nodes/node-11ce5d55e445/artifacts/catalog-targets.json`。首次运行在端口租约阶段失败，未进入业务；保留原件，没有终止未知进程。
- 地址解析与鉴权 16 passed；Gateway／消息流 HTTP 合同首轮 180 passed，唯一新文件根 fixture 错误修正后该组通过；Workspace 递归结构检查 9 passed；原 Provider 合同 9 passed。协议生成一致性及包含 18 份实际 sourceTransform 源码的真实类型检查通过；正式 Workbench 构建与本轮浏览器验收使用同一源码。命令、原输出与退出码位于 `out/tests/temp/quick_access_opener_20261102/artifacts/`。
- TODO：媒体、只读来源、Diff 的完整投影及独立版本参数、原 Chat／空文件编辑器真实调用方和 Electron 产物尚未验收，2.2／7.4 保持未勾选。

### 2026-11-02：Thread 稳定身份切片（2.1）

- Thread 编辑器以 Gateway／工作区／Session／Thread 构造唯一内部投影；匹配、分屏复制、关闭重开和版本 2 序列化均使用同一身份，临时 route 不进入正常恢复记录。恢复和原 resolver 重新查询当前登记，缺失／歧义拒绝，不自动连接、启动或改投活动工作区。同一输入的 Pane／导航在途解析共用 Promise，控制面重解不探测全部工作区服务。
- 新建、会话导航、fork、任务及资源页调用方已迁齐；旧无 Gateway 身份的记录原样保留为可关闭恢复错误，仅写新格式，不凭当前 Gateway 猜测。
- 定向 Bun 17 项通过；真实 Workbench TypeScript 检查零诊断；正式共享构建通过。新增真实 Gateway／Chromium 场景通过标签去重、分屏、草稿／每组视图刷新恢复、未知 Gateway 与歧义拒绝且不读取对应 Session。歧义只复制一条真实登记响应，属于明确替身边界；真实 route 改变／复用由定向检查证明，尚不能据此勾选 2.5 的完整产品场景。
- 原完整聊天工具循环回归通过，保留封存请求、文件保存、main／child 并行隔离、迟到导航和删除后刷新恢复的原断言。首次新增测试错误要求重开在已有资源之外再复制标签，按上游 HistoryService 的实际行为修正；随后修复替身回调清理竞态并重新通过。两次失败原件均保留。
- 类型检查原并行模式触及 4 GiB 外部保护；使用编译器单线程及局部 GC 预算后，在相同硬限制内约 50 秒完成。未调整用户环境或放宽内存上限。
- 命令、源码指纹、退出码与原输出：`out/tests/temp/quick_access_thread_20261102/artifacts/`；真实页面产物：`out/tests/integration/clients/web/test_thread_editor_identity/`、`out/tests/integration/clients/web/test_basic_chat_tool_loop/`。Electron 原生及剩余快速访问能力尚未验收，相关任务继续未勾选。

### 2026-11-02：目录模糊召回切片（3.1）

- 原 catalog 搜索及 Gateway 公开目录镜像共用顺序字符匹配和搜索键投影，名称、ID、逻辑路径及冻结 UTC 创建位置分别匹配。搜索键按原快照复用，保留排序、top-K、total 与 revision；旧子串语义的游标明确失效。导航 folder 不创建 Session 或 Thread。
- 定向 Python 56 项通过，覆盖首屏之外缩写、Unicode、逻辑路径、独立字段、分页、查询变化、重命名与删除；真实 Gateway 集成通过原后端创建／移动及在线、重启后离线镜像搜索，核对 request_id、归属、stale 状态及物理 locator 不输出。次工作区仍是既有明确 HTTP 替身，本项不证明新 Quick Access Provider 已接入。
- Pyright 零诊断，OpenSpec strict 通过。完整 Ruff 检查仅剩 HEAD 原有的 TRY004 两条和 ISC004 一条；新增导入格式已修正，排除这两项既存规则后检查通过，未改变既有异常合同。
- 原始命令、源码指纹、退出码和审查收据保留于 `out/tests/temp/quick_access_catalog_20261102/artifacts/`；真实集成产物位于 `out/tests/integration/gateway/test_session_catalog_search/`。本批无前端及公开 DTO 变化，未重复 Workbench 构建。

### 2026-11-02：Thread 有界发现切片（3.2）

- 新名称查询复用原 catalog 可见快照、Session 生命周期读准入和控制库只读事务；main 取冻结指针，child 取既有发布记录及标题。不加载 cold runtime、不读正文、不新增表或 schema 版本。每页明确目录／行数／结果／调度时间预算及独立 cursor，不承诺跨控制库同一时刻快照。
- 定向 Python 206 项、客户端 13 项通过；真实 Gateway 集成经产品创建、执行及 fork 建立同名 main／未打开 child／另一 Session main，验证后端重启续页、精确 URI、原 request_id、无额外模型调用，以及新 child 使当前分支旧 cursor 返回 409。
- 新模块 Pyright 零诊断，SessionService 19 条既存诊断与 HEAD 一致、无新增；Ruff 排除原 API 12 条 B008 后通过，另保存原件对比。生成一致性、Buf 格式／breaking、公开 descriptor／OpenAPI、真实 Workbench TypeScript 及生产构建均通过。
- 新集成装配初期遗漏游客登录、Gateway 回调地址和 native 辅助服务，并误用 source point 字段；逐项按原输出修复，未更改产品来迁就测试。端口曾被瞬时连接占用，改用既有进程租约提前预留本文件服务端口。失败原件与最终通过均保留。
- 证据：`out/tests/temp/quick_access_discovery_20261102/artifacts/`；正式产品查询原件位于 `out/tests/integration/gateway/test_thread_name_search/`。Provider 尚未消费本查询，3.3／3.4 及整体 Quick Access 验收仍未完成。

### 2026-11-02：文件根与轻量 stat 切片（4.1）

- 唯一文件投影保存稳定 Gateway／工作区归属及不透明规范根，迁齐所有旧叶子 query 消费者；根及特殊字符支持原版 joinPath／relativePath／序列化往返。每次操作解析当前已登记 route，旧临时 route 记录明确不可恢复。describe 只返回真实类型、size／mtime／ctime，不读正文或计算 hash；保存仍使用实际读取的 revision／CAS，stat 不推进基线。
- Python 168 项、客户端 3 项／37 断言、公开合同 391 项通过，真实 Workbench 类型检查与生产构建通过；生成一致性、Buf 格式／breaking、OpenAPI 和 OpenSpec strict 通过。真实 Gateway／Chromium 完整聊天工具循环保留文件树预览、原版编辑器打开／保存及多 Thread 的原断言并通过。
- 目录根由真实 FileService／provider＋显式 Gateway 传输 spy 和后端真实文件系统 describe 分层验证，尚不证明 SearchService 或完整搜索 UI；特殊字符最终修正由定向客户端、最终类型检查与构建覆盖。Pyright 10 条旧诊断与 HEAD 一致，无新增。列表 stat 最初触及原响应预算，收敛为 describe 专用后通过，未放宽预算。
- 准确命令、源码指纹、原输出、失败归因与结构审查：`out/tests/temp/quick_access_file_root_20261102/artifacts/verification.json`。2.2／2.4／4.4 等剩余完整任务保持未勾选。

### 2026-11-02：有界文件模糊召回切片（4.2）

- 同一导航 search 新增显式 fuzzy，按根内解码相对文件路径作大小写折叠顺序字符召回，只返回文件，不读正文；旧 path／glob／content 语义保持。共用原扫描、分页和预算，不加索引或第二查询链。取消释放调用方等待，原只读 worker 恢复后核对同一 deadline。
- 定向 Python 176 项通过，最终 7 项 fuzzy 检查通过；真实 Gateway＋共享 Backend 双挂载查询通过，覆盖首批 120 个无关文件之后的缩写、激活另一工作区仍定位原根、独立续页、模式旧 cursor 与扫描超限错误。尚不证明 Quick Access UI 或远端 SSH。
- 公开协议批次 426 项通过，另 1 项发现上一切片 stat 字段编号断言漏迁；按已批准合同补字段及可选值往返后，该文件 9 项全部通过。模式公开字段仍是 string，生成前端只更新注释；运行时客户端类型与代码未变，复用上一切片的最终真实类型检查和生产构建。
- Schema Pyright 零诊断；原 owner 的 10 条旧诊断与 HEAD 一致、无新增。Ruff、生成一致性、Buf 格式／breaking、OpenAPI 与 OpenSpec strict 通过。证据与失败归因：`out/tests/temp/quick_access_fuzzy_file_20261102/artifacts/verification.json`。

### 2026-11-02：文件文本选项与精确坐标切片（4.3 部分）

- 原导航 search 增加 typed search_options，贯通正则、大小写／smart case、整词与分隔符、多行、全部非重叠命中、include／exclude 及明确 ignore 模式。省略选项保留原敏感字面量和每行首个命中；旧匹配实现已被同一编译／遍历链替换，原精确工具产物 capability 也复用此链。模式按一次本机调用编译，正则执行消费剩余总期限。
- 多行起止继续提供码点坐标，并由原 owner 使用完整文本同时计算 UTF-16 起止列与片段起点列。裁剪前的 emoji 不再需要客户端猜测或额外读取正文；使用稀疏代理对位置复用转换，ASCII 跳过该扫描。完整 SearchService 和客户端坐标消费尚待接入，不据此勾选 4.3 或 4.4。
- 最终 209 项定向 Python 检查通过；真实 Gateway＋共享 Backend 双挂载再次通过，核对跨 CRLF／emoji 的正则命中、大小写、路径过滤、UTF-16 位置、精确读取与 request_id。公开合同先完成 428 项，追加 UTF-16 字段后定向协议／往返 37 项及真实公开结构导出再次通过；最终类型检查和生产构建通过。
- Ruff 与 schema Pyright 通过；原 owner Pyright 从 HEAD 10 条既存诊断减至 9 条，无新增。生成一致性、Buf 格式／breaking、OpenAPI 与 OpenSpec strict 通过。末次验证前导入排序问题已修复，失败原件保留。
- 此切片当时未接目标 ignore 文件及客户端搜索；后续唯一服务、上下文和目标 ignore 切片已迁齐。regex/wcmatch 直接依赖也已在修复 metadata 包发现后登记，未升级其锁定版本。
- 证据：`out/tests/temp/quick_access_text_options_20261102/artifacts/verification.json`。这些分层检查不代 Quick Access UI、远端 SSH 或 Electron 验收。

### 2026-11-02：唯一搜索服务与本地模型隔离切片（5.1～5.3）

- Workbench 创建前沿原 singleton 登记项目 RemoteSearchService 子类；同一文件 file／text Provider 连接原导航 API。只有明确业务根的正文组合使用项目分支，原生容器继续使用原服务。按完整 URI 在 findMatches／首次和后续 progress 前限制模型；零命中模型同样屏蔽同文件磁盘旧命中，不自动保存。
- 仅已打开查询在请求前选精确文件；文件名保留完整相对路径，复用上游模式匹配。分页复用每个 owner 的路由和固定请求合同，结果上限在模型与磁盘之间合并；取消／dispose 中止实际请求并拒绝迟到及同页后续进度。后端 UTF-16 源区间与有界首行预览分开投影，不再读取正文推算位置。业务 AI 明确未启用，未支持的选项在本地进度前拒绝。
- 最终 10 项搜索服务集成及原 Thread／文件检查合计 17 项、122 条断言通过；公共 Gateway／HTTP 客户端 65 项、153 条断言通过：真实 ServiceCollection／InstantiationService 与上游匹配／结果转换，Gateway 传输为明确 spy、文本为内存存取装具。覆盖 A／B 同路径、原生容器同名 URI、零命中覆盖、双向隔离、仅已打开、上限、分页、失败归属、取消及释放。不能据此声称真实 IndexedDB、SearchView、Quick Access UI 或 Electron 已完成，5.4～5.6 保留未勾选。
- 真实 Gateway／Chromium Thread 身份回归和同源码正式生产构建通过。首次页面等待暴露在线模型目录刷新阻塞，原后端日志有前台等待记录；测试明确改用本地目录。第二次在启动前遇临时端口占用；接入现有端口租约保留绑定后，原五秒断言和全部打开／恢复场景通过，未终止未知进程。服务装具初次遗漏上游 Worker 服务，补齐为误入即失败的明确依赖后通过。所有失败原件保留。
- 最终真实 Workbench 类型检查、Ruff 和 OpenSpec strict 通过；本批没有公开 DTO 或协议生成变化。证据：`out/tests/temp/quick_access_search_service_20261102/artifacts/verification.json`；正式服务及浏览器产物分别位于 `out/tests/integration/clients/web/quick_access/search_service.test/` 和 `out/tests/integration/clients/web/test_thread_editor_identity/`。

5.6 验收：真实 Gateway／正式 Workbench 中 A、B 同路径不同 dirty 模型及 IndexedDB 同名文件，普通／仅已打开／转交／移动视图／反向 B 均保持归属；两份磁盘正文不变。产品入口切换导航并等待选中，避免测试 API 激活与界面镜像的布局恢复竞态。16 项定向检查、实际生成源码类型检查、Ruff 及 fixture 正式构建通过。命令、源码指纹与失败原件见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/isolation-verification.json`。Search Editor 与 Electron 尚未验收。


### 2026-11-02：Search Editor 独立范围与持久化切片（6.3～6.6）

- 在原配置模型保留版本化 scope，同一 sourceTransform 贯通原 factory、命令、控件更新、copy、JSON serializer 和磁盘／backup header；priorConfig 不继承范围。原 Pane 子类使用每个实例的 child QueryBuilder，实际原搜索取消、模型 await 与最终写回均核对输入／搜索代际，不改私有 readonly 上下文或吞构建错误。
- 真实 Gateway、A/B 托管后端、正式 Workbench 与 Chromium 验证分屏独立查询、不同范围不复用、复制、另存、关闭重开及刷新恢复；实际请求只访问对应 owner，dirty 正文恢复且两份磁盘文件均未保存。保存结果经原扩展双击定位仍打开原 A 文件。旧格式显示历史正文且只读，重查明确报错且无 HTTP；经原 Quick Input 明确选择 B 后才查询 B。损坏／未知版本在原编辑器占位页显示具体错误，均未发业务搜索。
- 实际跳转验收暴露原搜索结果模型未接入文本解析引用，词语高亮会报 unable to resolve；在原 startup contribution 登记同一结果模型的内容 Provider，由原 Input 持有解析引用并随其释放，移除直接销毁模型的旧生命周期。保留高亮、定义跳转和原结果模型，不增加正文副本。初次范围名称回退为 ID、通用错误占位隐藏原因等失败已按原件定位修复，未放宽断言。
- 最终 14 份转换源码的真实类型检查、23 项定向检查／178 条断言、Ruff 和 fixture 正式生产构建通过；结构审查无 findings，固定 upstream 原件不变。准确命令、源码指纹、原始输出、退出码和失败原件见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/editor-verification.json`。
- 6.2 保留未勾选：两个 rawData 分支已携带范围，但需要上下文行的重跑分支仍被现有文件 Provider 明确拒绝，尚无完整运行证据。6.7 保留未勾选：真实深度 backup 故障恢复、route 变更／复用和迟到 Pane 完成仍需专项验收；本批不证明远端 SSH 或 Electron。

### 2026-11-02：同次读取上下文与完整转交切片（6.2）

- 原导航 search 增加 0～100 行上下文，文件及精确工具产物沿同一匹配器、读取、分页及预算返回有类型的行；上下文每页去重，不占命中数，仍计入响应字节。客户端按最终接受的命中合并跨页上下文，剔除命中行、上限之外的无关行及已被本地模型接管的旧正文，截断可见；未增加逐命中读文件或第二搜索链。
- 真实 Gateway／Chromium 验证 contextLines=0 原结果转交时无额外搜索、contextLines=1 对未打开磁盘文件自动重查，以及 dirty 模型上下文、相邻命中去重。保留既有分屏、复制、保存恢复、旧格式只读和 A/B／IndexedDB 隔离断言，实际 HTTP 仍指向 A；两份磁盘正文均未保存。恢复布局过早捕获无范围错误已修复为首次查询同步捕获，后续查询范围继续固定。
- 后端及协议定向 321 项、搜索服务 12 项／70 条断言、14 份实际转换源码类型检查、Ruff、正式生产构建、生成一致性和 Buf 格式／依赖／真实提交 breaking 均通过。首次类型检查遗漏默认值、Ruff 局部命名和真实页面恢复范围失败的原件保留，按具体原因修复后复验。
- 真实工作区 HTTP 两项及跨 Gateway／localhost SSH 定向一项通过，后者经连接入口和目标 Workspace 两条原路由核对上下文 URI、CRLF／BOM 行号和 request_id。迁齐此前 stat／完整坐标的旧断言，并修复该场景残留的旧装具模块导入及已移除的 artifacts_dir 构造参数；产物直接复用现行 harness 的逐节点目录，不增加兼容装具。原失败与最终通过分别保留在 context-http-*、context-federation-* 收据中。
- 准确命令、源码指纹与退出码：`out/tests/temp/quick_access_scoped_query_20261102/artifacts/context-*.json`；最终真实页面：`out/tests/integration/clients/web/quick_access/test_text_search/runs/run-00bb9f01ab54/nodes/node-f4e408bf0b21/artifacts/search-result.json`。4.3 的目标 ignore 文件及 6.7 的深度恢复／route 专项仍未完成，不将其或 SSH／Electron 标记为已验收。


### 2026-11-02：目标机器 ignore 与完整搜索选项切片（4.3）

- 同一文件 owner 以 pathspec 解释所选根以内的 `.gitignore`／`.ignore`；支持继承、锚定、否定和 `.ignore` 优先级，目录在递归／正文读取前剪枝。显式文件与已打开模型沿原上游行为，不为磁盘规则增加请求或第二套 TS 引擎；父目录／全局 ignore 仍在 progress 和 HTTP 前明确拒绝。
- 规则累计读取受原 max_bytes／deadline 约束，每目录只读一次；stat 与缺失事实进入原 cursor 前缀，隐藏规则被改写、创建或删除同样使旧页失效，不新增全文 hash。提取共同有界读取，文件 read／search／规则共用同一 I/O 链。
- 修复 setuptools 扫描 JS 依赖的 metadata 故障，明确 Python 包范围；只新增 pathspec，登记已有 regex／wcmatch 直接依赖，没有升级既有包。proto 注释与生成产物同步，无字段、配置版本或数据库变化。
- 334 项后端／协议检查通过；23 项工作台服务／范围／持久格式用例通过（22 项首次通过，再补拒绝断言后服务文件 14 项通过，沿用其它 9 项）。真实类型检查含 14 份 sourceTransform、正式 Workbench 构建、Chromium 原页面通过，默认 respect 隐藏文件、原设置关闭后 none 显示文件；A/B dirty、IndexedDB、仅已打开、SearchView、Search Editor 和上下文原断言保持。
- 真实跨 Gateway 与 localhost SSH 定向用例通过：A/B 同名规则相反，两个真实路由均按 B 规则搜索，关闭 ignore 返回两个文件。前三项 Web／HTTP 验收在组合运行中已通过，组合在最后一项触发 240 秒外部超时；保留退出码 124，仅重跑未完成的远端用例并通过（42.36 秒），本次孤立 SSH 进程已按运行 ID 清理。
- Ruff、Buf format、生成一致性与相对 77fcd4379 的 breaking 检查、OpenSpec strict 通过。结构审查无 findings：规则模块只负责纯匹配，I/O 仍归文件 owner；触达源码目录各 12／10 个文件，导航 757 行仍为单一有界只读链。
- 准确命令、源码指纹、原输出和退出码：`out/tests/temp/quick_access_scoped_query_20261102/artifacts/ignore-*.json`；页面：`out/tests/integration/clients/web/quick_access/test_text_search/runs/run-5c568eae5d7f/nodes/node-f4e408bf0b21/artifacts/search-result.json`。4.4、6.7 以及 Electron 仍需各自完整验收。

### 2026-11-02：Search Editor 深度恢复与路由变更验收（6.7）

- 沿原 Monaco 输入写入 A/B 独有未保存结果，只读核对原 IndexedDB backup 的正文及范围；刷新后正文原样恢复且没有自动搜索。延迟真实控制面回执期间替换另一组的 Input，放行后查询仍属于 B。
- 正式移除 B 后搬迁隔离装具，由 C 占用旧路径及 route：B 重查明确报错且零文件搜索 HTTP。再经管理入口注册原 B，新 route 保留稳定 workspace ID；再次查询及刷新恢复均只访问新 route 和原规范根，不显示 C 正文。保留此前复制、另存、关闭重开、dirty 模型、上下文及 A/B/IndexedDB 原断言。
- 最终真实 Gateway／正式 Workbench 构建／Chromium 场景通过（156.85 秒），Ruff、JS 语法及 OpenSpec strict 通过。仅新增测试，无产品代码变化，复用上一切片真实类型检查。结构审查无 findings：专用 helper 约 150 行，无新增源码目录。
- 三次失败均为验收装具问题：删除遗漏 runtime group 前置条件、正文输入焦点错误、将注册摘要当作完整工作区 DTO；分别按原输出修正，未放宽身份、正文或零请求断言。准确命令、指纹、失败原件及退出码位于 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/recovery-*.json`；最终页面原件位于 `out/tests/integration/clients/web/quick_access/test_text_search/runs/run-9421a9a007bb/nodes/node-f4e408bf0b21/artifacts/search-result.json`。本项不证明 Electron 或剩余 Quick Access 功能。

### 2026-11-02：文件边界与坐标验收（4.4）

- 在原真实 Gateway／受管 Backend 场景补齐缺失目录的 describe／有效 search、未知二进制的无正文 stat 与明确编码错误、只读 agent-spec 拒绝普通文件写入且原件不变。根内 include、大小写不敏感多行正则、CRLF／emoji 的码点与 UTF-16 完整区间均核真实 HTTP 回执；保存后 describe 只返回 stat，旧 revision 仍以 409 拒绝，保留原 request_id。
- 真实 HTTP 场景通过（67.76 秒）；原版 FileService 定向 3 项／37 断言通过，确认目录／二进制 stat、稳定投影及 route 变化后的 CAS 基线。复用前两切片的查询选项、dirty 模型和真实页面证据，无产品变化，不重复前端构建／类型检查。Ruff、差异检查及 OpenSpec strict 通过；原文件 522 行，新增断言仍属同一导航场景，无结构 findings。
- 首两次失败来自测试误把未知 `.bin` 当成受支持媒体；按既有合同同时核 text／auto 都明确拒绝，未扩展媒体能力。第三次缺失目录搜索漏传必需 query，修正有效请求后通过。原输出、源码指纹、命令及退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/file-boundary-*.json`；真实回执保留在 `out/tests/integration/backend/agents/test_resource_navigation/artifacts/navigation-receipts.json`。这不代替尚未实施的统一 opener 或 Electron 验收。

### 2026-11-02：文件观察去除正文轮询

- 删除每个编辑器文件每五秒读取完整正文的轮询，复用原共享 watcher／SSE。相同完整根共用订阅，事件先按规范根及实际监听目标限制，父目录删除及溢出仍通知原 FileService；最后释放才取消，路由解析晚到不再建立订阅，流错误沿原 watch 错误通道暴露。通知不读取正文、不更新 revision／CAS。参考 `reference_repo/vscode/extensions/copilot/src/platform/filesystem/vscode/fileSystemServiceImpl.ts` 的原文件 watcher 接入。
- FileService 定向 5 项／51 断言、真实类型检查（含 14 份转换源码）、Ruff、JS 语法通过。真实 Gateway／正式 Workbench 构建／Chromium 完整场景通过（162.79 秒）：空闲六秒正文 GET 增量为零，外部真实文件修改自动显示，dirty 草稿保存由原 FileService 明确拒绝且磁盘不被覆盖；原后端 CAS 单测及原双工作区／备份／路由恢复断言保持。结构审查无 findings：产品及服务测试各约 155 行，页面 helper 约 50 行，无新源码目录。
- 首次类型检查的闭包收窄、单测误比较 URI 内部缓存、页面装具目录变量被接口对象遮蔽均按原输出修正，未放宽身份或冲突断言。命令、源码指纹、原输出及退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/file-watch-*.json`；最终页面为 `out/tests/integration/clients/web/quick_access/test_text_search/runs/run-191eb29762e2/nodes/node-f4e408bf0b21/artifacts/search-result.json`。不增加任务勾选，不代表默认 Quick Access 或 Electron 已实现。


### 2026-11-02：默认名称入口（7.1、7.2）

- 原贡献仅登记一次项目默认 Provider，仍用原 Picker、fuzzy scorer、历史、打开与编辑器组；按最近／Session／Thread／文件固定分组。空输入不发名称或正文查询，关闭历史后不枚举补空。各类别渐进发布，使用稳定目标保持活动项；Session 接受时由原 owner 解析 main，Thread 保留精确身份。
- 真实 Gateway／正式 Workbench 页面通过：A／B 查询目标隔离、延迟真实 Thread 响应时其余分支先到、未打开 main 的 ID 发现、键盘打开文件的 2:3 位置和多层路径、无匹配不扩搜、Command Center 帮助不查询、真实设置关闭历史。原 dirty、watch、IndexedDB 隔离、SearchView、Search Editor 分屏／保存／backup／route 场景全部继续通过；最终页面为 `out/tests/integration/clients/web/quick_access/test_text_search/runs/run-a5f66c4a83d6/nodes/node-f4e408bf0b21/artifacts/search-result.json`。
- 原 PickerEditorState 在 hide/dispose 与异步 restore 交错时读取被清空的状态；同一转换内改为 await 前消费局部状态，时序测试证明 dispose 可恢复，旧恢复不清掉下一次定位状态。固定 16 份源码的全部锚点、上游原件和真实类型检查通过。
- 9 项定向检查／100 条断言通过。delegate／filter／补充项／handleAccept 在浏览器组件装具中验证空查询、URI 查询、取消／迟到响应、后台标志和原接收者；宿主与 Picker 事件是明确替身，不代替空文件编辑器或原 Chat 二级流程的完整验收，7.4／7.6 仍未勾选。普通页面通过后仅补齐 delegate 空输入分支，其余页面分支等价，定向检查与最终构建覆盖该增量。
- 设置验收失败原件确认旧装具逐键插入触发自动补括号，生成无效 JSON；现以真实剪贴板粘贴和保存，并只读实际存储核对完整 JSON，未绕过设置 owner。JS 静态检查、Ruff、差异检查通过；准确命令、源码指纹、原输出、退出码及保留的失败原件位于 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/default-access-*.json`。多范围、通用 URI opener、完整帮助转接、正文和 Electron 等剩余场景继续未完成。


### 2026-11-02：帮助与模式转接（7.5）

- 原 Controller 在浮层显示前捕获发起归属，原输入前缀转接继续传递 typed options；原 HelpQuickAccess 的确认和精确前缀两个 show 直接传递 options，返回默认入口关闭本次递归帮助。独立新命令重新捕获自己的上下文，不从之前浮层隐式继承回调或错误；发起错误在显式转接中不改投后来焦点。
- 真实 Gateway／正式 Workbench 经原设置保持浮层，将底层编辑器从 A 切到 B，经 Command Center → `?` → `%` 查询仍只访问 A，返回默认入口也只显示 A 历史且不递归帮助。原默认、dirty／watch、SearchView、Search Editor 分屏／保存／backup／route 场景继续通过；页面原件：`out/tests/integration/clients/web/quick_access/test_text_search/runs/run-a9be95922242/nodes/node-f4e408bf0b21/artifacts/search-result.json`。
- 11 项定向检查／126 条断言通过，原 Help 两个处理器的接收者回调保持同一对象；后者为浏览器组件装具，不冒充真实原 Chat widget 验收。18 份实际转换源码类型检查、正式页面构建、Ruff、JS 静态检查和结构审查通过；第一轮类型检查指出嵌套 options 字面量的额外属性，改为原 typed 局部变量后通过，未逃逸类型。
- 准确命令、源码指纹、原始输出、退出码与失败原件见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/invocation-verification.json` 及相邻收据。本切片不完成 7.4／7.6、跨范围、通用 URI opener 或 Electron 的剩余验收。

### 2026-11-02：列表与打开动作验收（7.3）

- 真实 Gateway／正式 Workbench 页面核对 Session、Thread、文件的名称、工作区归属、详情、图标及实际命中高亮；悬停 Tooltip 和 Tab 聚焦的侧边按钮均通过。明确开启原 Quick Open 预览设置后，侧边文件显示预览标签；两次原后台接受保留输入焦点，文件固定在发起组，原组 Thread 保持可见。移除历史后原标签与后端 Session 均保留。
- 最终页面通过（184.12 秒），原 A/B／IndexedDB、dirty／watch、SearchView、Search Editor 分屏、保存、backup 和 route 恢复断言继续通过，无页面错误。仅新增测试，复用上一切片同产品源码的真实类型检查；本轮 fixture 重新完成正式构建。JS 语法、Ruff、结构及 OpenSpec strict 检查随本边界保存。
- 前两次失败分别是装具未先悬停便查找通常隐藏的按钮，以及未开启预览设置便断言预览；依据原列表 CSS 与原设置默认值修正，未修改产品行为或放宽归属断言。命令、源码指纹、原始输出及退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/list-*.json`；最终页面原件：`out/tests/integration/clients/web/quick_access/test_text_search/runs/run-0ef312a76ada/nodes/node-f4e408bf0b21/artifacts/search-result.json`。7.4／7.6、统一 URI 打开和 Electron 等仍未完成。

### 2026-11-02：符号来源与搜索边界（5.5）

- 原 `#` 登记只选择项目符号子类，复用原 Picker 的 provide、过滤及释放；明确业务 owner 或发起身份错误时，在全局符号查询之前返回未启用或原错误。原生无业务范围调用继续使用原符号服务，文件内符号不受影响。默认入口开启 includeSymbols 时明确提示当前缺少可限定业务根的服务；未增加全局设置改写。
- 26 项定向检查／212 条断言通过，覆盖根与 extras、AI 拒绝、等待越过原 200ms debounce 后全局符号 Provider 零调用，以及原生符号调用仍到达真实注册表。真实 Gateway／正式构建／Chromium 完整场景通过（181.25 秒），`#` 保留 B 身份且没有领域查询，原 A/B、dirty、watch、SearchView、Search Editor、backup 和 route 断言保持。复用同产品源码指纹的 18 份转换源码真实类型检查；本次构建由正式 fixture 完成。
- 合同浏览器装具改以本地静态服务加载同一编译产物，去掉每例经控制通道传输数 MB base64；相同 26 项从 20.94 秒降至 4.74 秒，没有放宽期限或断言。原列表复用时隐藏但保留旧 detail，展示测试现先按候选身份限制再核文案，仍要求唯一目标。端口 TIME_WAIT 导致的启动失败与前两次定位失败原件保留；确认端口重新可绑定后再跑通过，未终止未知进程。
- 准确命令、源码指纹、原始输出和退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/symbols-*.json`，最终为 `symbols-contract-004.json`、`symbols-browser-004.json`；文本定位其余模式与 Electron 尚未独立验收，不勾选 10.2 或桌面任务。

### 2026-11-02：明确多工作区范围（8.1、8.2）

- 普通混合入口复用原 Quick Input 多选已登记工作区，展示 Gateway、稳定工作区身份及运行状态；确认才查询，取消恢复原查询和活动候选身份。只选择调用方、delegate 和 quickNavigate 保持原合同，不增加范围按钮。模式转接保留用户明确选择的集合，发起编辑器归属另行保留。
- 所选 owner 共享一次路由读取，使用原 Limiter 将业务分支及根请求限制为三路并发；按类别交错调度，取消同时释放在途与排队分支。停止目标只展示不可查询，不调用启动、连接、激活或注册。Session 打开使用命中分支的 owner，最近记录仅包含明确选定集合。
- 最终真实 Gateway／正式 Workbench 构建／Chromium 场景通过（208.49 秒）：勾选未确认零查询、取消保持 A、确认仅查 A+B、C 零业务查询、三条在途请求均实际中止，释放旧响应不覆盖新结果，`?` → `%` 保留 A+B；选停止 C 不调用生命周期接口，其余两者仍可查询。原 dirty、watch、SearchView、Search Editor、保存／backup／route 恢复断言保持，页面错误为空。页面原件：`out/tests/integration/clients/web/quick_access/test_text_search/runs/run-44393c58118d/nodes/node-f4e408bf0b21/artifacts/search-result.json`。
- 真实类型检查包含 18 份转换源码，通过后核对 881 份产品源码指纹一致；26 项定向检查／217 条断言、Ruff、JS 语法、差异检查及 OpenSpec strict 通过。结构审查无 findings：范围选择独立于查询编排，无新增源码目录。准确命令、指纹、原输出和退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/scope-types-002.json`、`scope-browser-004.json`、`scope-final-contract-001.json`、`scope-final-static-001.json`。
- 前次失败原件保留：类型字面量额外字段、候选身份定位、虚拟列表末尾状态定位，以及读取被取消响应正文的装具时序均按具体证据修正；未放宽身份、并发或零请求断言。8.3～8.6 尚需身份代际、状态展示、文件续页及完整错误矩阵，不因本切片部分覆盖而勾选；Electron 仍未验收。

### 2026-11-02：Session／Thread／目录默认动作（3.3）

- Session 接受复用原 main-thread owner，Thread 保留精确 child；catalog folder 不再被过滤，默认动作打开原会话导航，重新读取最新 breadcrumb 并复用原分支加载，等待真实按钮挂载后定位。新请求、关闭视图及过期回执释放旧定位，不创建编辑器、Thread 或执行。
- 独立真实 Gateway／正式 Workbench 构建／Chromium 验收通过：同名 Session、main 与尚未打开的 durable child 分别可发现；Session 打开 main，child 保持身份；查询后移动 folder 会定位新祖先，侧栏关闭后重新打开，删除后的旧候选明确失败，B 的同名 folder 不被改投。8 条名称查询和 55 条 request_id 回执留存，标签数量、后端 Thread 列表及零消息／job 写请求均核验，页面错误为空。页面原件：`out/tests/e2e/clients/web/test_quick_access_catalog_targets/runs/run-55d121cf8b58/nodes/node-11ce5d55e445/artifacts/catalog-targets.json`。
- 18 份实际转换源码的真实类型检查、32 项合同检查／225 条断言、Ruff、JS 语法、差异检查及 OpenSpec strict 通过。最终 E2E 用时 100.33 秒（进程总计 103.23 秒），源码指纹未变化。准确命令、指纹、原输出与退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/catalog-target-{types-002,contract-001,static-002,browser-002}.json`。结构审查无 findings，无新增源码目录或第二目录加载链。
- 首次浏览器失败源于装具在初始化 HTTP 完成前启动 5 秒 Composer 等待；改为由点击触发并等待精确 bootstrap HTTP 回执、核 owner／status／request_id，随后仍使用原 5 秒验证 Composer。原件保留，未改产品或放宽身份断言；实际 child 初始化约 3517ms，main 约 938ms。本切片未重跑约 200 秒的完整 text_search 场景，不代表 3.4、8.3～8.6 或 Electron 已验收。

### 2026-11-02：各 owner 明确续页与失效确认（8.5）

- 文件名称查询不再由 SearchService 隐式连续取页；同一 SearchService／文件 Provider 的原循环提供单根单页入口，每次最多 50 项，独立透传原 cursor，用户明确续查才取下一页。普通 file/text 搜索仍使用同一循环按原合同聚合，未新增扫描器、索引或正文 hash。分支排队时立即移除续查动作，阻止重复排队；续查失败清除该分支旧页，显式从首屏重查，其他分支保留。活动目标失效后持续清空确认，后续结果不能自动接替原 Enter 目标。
- 正式 Workbench 构建／真实 Gateway／Chromium 通过（111.90 秒，进程总计 114.85 秒）：55 个文件按 50＋5 返回；55 个 Session 由产品创建 API 生成，其 catalog 与 main Thread 分别沿独立 cursor 取到 50＋5。拖动原列表滚动条后续页仍保持原活动文件；删除已扫描文件使原 cursor 明确返回 400，旧页消失，重查只访问文件分支，新的同位置文件未被自动选中，Enter 不打开替代对象。14 条查询回执均核验 request_id，只访问 A，B 零查询，无消息／job 写请求，页面错误为空。最终仍只挂载 7 条可见行。页面原件：`out/tests/e2e/clients/web/test_quick_access_catalog_targets/runs/run-9c5d5ac1ea87/nodes/node-11ce5d55e445/artifacts/catalog-targets.json`。
- 28 项定向检查／236 条断言、18 份实际转换源码的真实类型检查、正式构建、JS 语法、Ruff、差异检查及 OpenSpec strict 通过。类型检查期间仅测试驱动改动，882 份产品源码指纹与最终验收一致。准确命令、原输出、指纹与退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/pagination-{types-001,contract-001,product-fingerprint-001,browser-005}.json` 及相邻 static／structure 收据。结构审查无 findings：搜索目录仍为 15 个 TS 文件，单一 Provider 与查询编排职责保持。
- 四份失败页面原件保留：测试定位器同时命中隐藏树、滚轮未到末尾，以及两处异步更新后状态行重新进入虚拟列表的等待顺序。依据真实 DOM 和 HTTP 回执修正为原版滚动条与完成回执后定位，未放宽身份、页数、零请求或业务期限断言。最末文件验收首／续页约 191／182ms，响应约 10.7／1.5KiB；仅说明本工作负载，不承诺总体性能。参考 Copilot 的原搜索服务、限量与取消职责；原目录变化校验保留以履行 cursor 合同。8.3、8.4、8.6、完整 text_search 回归及 Electron 仍未验收。

### 2026-11-02：查询生命周期与独立状态（8.4、8.6）

- 名称查询按原 Limiter 保持三路并发，原 JSON 十五秒期限覆盖排队、目标解析与该页读取；取消不发送排队请求，迟到响应不发布，超时结束 busy。文件查询复用原十秒总期限并取上层剩余期限，跨分页和 Gateway 透传同一 deadline，已经过期时不再解析路由或访问目标。分支分别展示完成／受限／失败及覆盖说明、实际 request_id；重试只读失败分支，续页失败撤下对应旧页，其余结果保留。
- 原 HTTP 身份屏障发布失效通知，原工作区上下文通知 Gateway 变化；旧候选、活动选择和在途读取立即失效，继续输入也不沿旧身份请求。不建立第二身份缓存或公开搜索 DTO。目录名称 owner 仍全目录扫描，尚缺服务端总预算，代码已登记 TODO，因此 **8.3 继续未完成**；通用 URI opener 的 2.2 也未完成。
- 新增真实 Gateway／正式 Workbench／Chromium 场景通过（135.20 秒）：产品 API 创建隔离 A/B/C 的关键状态，核只 A／明确 A+B、C 零查询、三条真实请求中止及第四分支不发送、迟到结果废弃、真实十五秒超时保留其它结果、全失败不伪报无匹配、单分支重试、停止目标零生命周期操作及 401 撤下旧身份。503／401 为明确 HTTP 故障注入，延迟沿原真实响应，属于 Integration，不冒充真实后端故障。页面错误与禁止写请求均为空；原件为 `out/tests/integration/clients/web/quick_access/test_query_lifecycle/runs/run-d020c09db7af/nodes/node-6c413386faf0/artifacts/query-lifecycle.json`。
- 原 catalog／文件独立分页 E2E 继续通过，旧 cursor 与 Enter 不改投的原断言保留。最终完整 text_search 回归通过（225.37 秒；包装进程 228.22 秒），包含 A/B／IndexedDB、dirty／watch、默认入口、范围取消保留明确活动身份、SearchView、Search Editor 分屏／保存／backup／route 恢复，页面错误为空。原件为 `out/tests/integration/clients/web/quick_access/test_text_search/runs/run-ce56a5c10600/nodes/node-f4e408bf0b21/artifacts/search-result.json`。旧测试先修虚拟列表观察，再依据鼠标滚动条点击会清空列表焦点的实际失败改用真实滚轮；未修改产品、缩减断言或延长期限，失败原件均保留。
- 77 项定向检查／295 条断言、18 份真实转换源码的类型检查、正式构建、Ruff、JS／TS AST、结构审查及 OpenSpec strict 通过。767 份产品文件核对仅新增目录预算 TODO 注释，其余与先前通过的类型／页面证据一致。类型检查默认采用编译器原生单线程，避免并行检查撞四 GiB 保护；独立提交 `34f43a07d`。试验构建复用插件没有生效，已移除临时插件，正式页面仍由既有共享 fixture 实际构建，没有宣称跨运行复用。
- 准确命令、源码指纹、原输出及退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/branches-{contract-002,types-003,browser-003,regression-001,regression-004,structure-003,product-evidence-001}.json` 及相邻日志。未重跑不受新变化影响的已通过场景；Electron 与其余任务继续未完成。


### 2026-11-02：名称查询服务端总期限（8.3）

- catalog／Thread 名称 API 接收可选有限 UTC 截止时间，取客户端剩余期限与十五秒的较小值；Gateway 透明透传。原 catalog 冷快照逐行读取、投影、匹配和发布前检查同一期限，未完成快照不缓存；SQLite 游标在超时退出前关闭。Thread 准入等待消费剩余期限，原一秒调度分页及受保护 worker 结算规则保持，不宣称强行打断原生阻塞 I/O。
- 386 项后端定向检查通过：已过期请求零扫描、冷读取中途停止、暖缓存匹配中途停止、远期时间戳不能扩大上限、真实准入等待超时后可继续查询。首轮唯一失败为测试 breadcrumb 包装函数未迁齐 check 参数，迁齐后通过，失败原件保留。90 项前端合同检查／332 条断言、18 份实际转换源码的真实类型检查通过；OpenAPI 两份快照同步登记 Header。
- 正式共享 Workbench 构建／真实 Gateway／Chromium 两场景通过（206.76 秒，包装进程 209.73 秒）：两个名称端点真实返回过期 409 及同一 request_id；普通分支和单分支重试都传剩余期限，继续核三路取消、迟到响应、十五秒超时、A/B/C 隔离、身份失效、目录定位、精确 child、独立分页和旧 cursor。生命周期页面记录 47 条查询，页面错误与禁止写请求为空；原件位于 `out/tests/integration/clients/web/quick_access/test_query_lifecycle/runs/run-4f9cc6b3b59b/nodes/node-6c413386faf0/artifacts/query-lifecycle.json` 和 `out/tests/e2e/clients/web/test_quick_access_catalog_targets/runs/run-4f9cc6b3b59b/nodes/node-11ce5d55e445/artifacts/catalog-targets.json`。
- 定向 Ruff、Python AST、结构审查及 OpenSpec strict 通过；API 原有 12 条 B008 按提交前后诊断逐项核对，无新增告警，未扩大修改。无新增源码目录、第二查询器或业务状态库。准确命令、指纹、原输出及退出码见 `out/tests/temp/quick_access_scoped_query_20261102/artifacts/budget-{owner-final-001,contract-001,types-001,browser-001,static-001,structure-001,spec-001}.json`；类型及页面运行期间只有不参与构建的后端测试变化。未重跑不受此次变化影响的完整 text_search，Electron 和其它未勾选任务仍未完成。
