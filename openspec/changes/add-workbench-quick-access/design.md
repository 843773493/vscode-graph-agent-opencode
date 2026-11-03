## Context

本稿记录已确认的产品概念、各功能接入与架构边界，不是实施顺序。已有证据来自当前工作树和固定 Code OSS 上游源码，未逐项做浏览器验收。默认结果与范围、六类交互基准、正文覆盖、索引归属、按需 URI、引用方式和接收者规则均已由用户确认；技术接入细节按这些原则细化，不再作为尚未回答的产品分歧。任务清单按完整业务链与验收职责组织，只声明必要依赖，不指定排期或宣布产品已完成。

### 概念目录树

```text
快速访问（Ctrl+P）
├── 找目标
│   ├── 资源：会话、Thread、文件、变更、Skill、配置
│   ├── 位置：行号 :、当前符号 @、工作区符号 #、内容 %
│   ├── 命令：>，例如“暂停当前任务”
│   └── 帮助：?，查看可用入口
├── 确定目标
│   ├── 名称、摘要、状态：帮助辨认
│   ├── Gateway／工作区／会话：明确归属
│   └── 规范资源 URI 或明确领域身份：精确定位
└── 选择并执行动作
    ├── 打开：当前组／侧边分屏、预览／固定、会话／文本／Diff
    ├── 引用：复制 URI、附加到聊天
    └── 管理：开始／暂停／继续、编辑／删除
```

“打开、引用、管理”是并列动作，不是三个必须依次完成的步骤。资源确认后可直接暂停任务而不打开详情；命令入口可以先选命令，再按需补选目标或直接使用已经明确的上下文。

| 概念 | 用户的问题 | 数据与执行归属 | VS Code 接入机制 |
|---|---|---|---|
| 找目标 | 我在找什么、在哪个范围找？ | Gateway 联邦调度、工作区各领域查询 owner；前端展示候选 | Quick Access、Quick Input、SearchService |
| 确定目标 | 同名结果中到底是哪一个？ | 规范资源 URI、独立稳定身份、明确归属 | 结果描述、分组、上下文与资源解析 |
| 打开 | 在哪里、以什么视图显示？ | 编辑器／视图 owner，保留目标归属 | OpenerService、EditorResolver、EditorService、编辑器组 |
| 引用 | 将哪个资源交给谁？ | 资源 owner 与接收 Thread／Composer，分别固定 | 同一选择器的只选择模式、命令 |
| 管理 | 对这个对象执行什么操作？ | 既有后端命令及状态机 | Action／Command、菜单、快捷键、上下文条件 |
| 公共体验 | 如何快速选、退出和再次找到？ | 原版展示态；业务事实仍在后端 | 模糊匹配、历史、取消、主题、设置、无障碍 |

### 已确认的默认入口与搜索范围

- 无前缀 Ctrl+P 统一搜索会话、Thread 和文件，使用分隔项按类型分组；空输入显示最近打开的资源。空输入最近记录也限制在所选范围；分组、排序与去重使用下文已定规则。
- 默认搜索范围优先取打开入口前焦点业务编辑器所属工作区；没有业务编辑器时，取导航当前工作区，并明确展示所选范围。要在 Quick Input 自动获得焦点之前捕获发起上下文，不能因输入框已聚焦而丢失原编辑器归属。
- 业务文件、Diff 或 Thread 编辑器通过各自已有目标归属确定工作区，不以整个 Workbench 的 IndexedDB 默认根冒充后端工作区。没有可确定范围时明确提示用户选择，不能猜测同名工作区。
- 选择器已打开后，不因用户切换标签而静默改变本次范围或已选目标；主动切换范围须明确更新范围展示并废弃旧查询结果。
- 搜索只访问用户当前选定范围。没有命中就显示没有命中，不自动扩大到其它工作区或 Gateway，也不先查询更广范围再在前端隐藏结果。只有用户明确选择更多范围，才开始对应的查询；超时、失败或结果受限同样不能触发自动扩大。
- 明确传入多个工作区或“当前 Gateway 全部工作区”属于合法范围，不违反禁止自动扩圈。“相关工作区”由独立控制面发现得到明确集合，再查询该集合；不会按工作区名称相关性预先裁掉用户已指定全部工作区中的内容搜索目标。

### 六类交互设计基准

以下完整保留用户确认的核心思路，是后续设计和疑问处理的共同依据，不是仅供参考的可选功能清单。每个接入的 Provider 均应核对适用行为；表中“需要 Provider 启用”“通常”“可配置”等条件按原版机制保留，不扩张成所有模式无条件相同。尚未确定的项目产品选择不能推翻这些共同概念；若确需偏离，应明确指出冲突、原因和影响再讨论。

| 类别 | VS Code 可参考的细节 | 项目中的用法 |
|---|---|---|
| 输入 | 输入框自动获得焦点 | 打开后直接输入，不再点一下 |
| 输入 | 提示文字随当前模式变化 | 明确提示正在搜会话、文件还是命令 |
| 输入 | 前缀切换模式，并提供 `?` 帮助 | 不要求用户背完所有入口 |
| 输入 | 名称、路径等支持模糊匹配和命中高亮 | 搜索结果解释“为什么匹配” |
| 列表 | 名称、描述、详情分层展示 | 名称突出；工作区、Session 等归属放次要位置 |
| 列表 | 用分隔项分组 | 区分最近打开、会话、Thread、文件 |
| 列表 | 图标表达对象类型 | 同名会话和文件也能快速分辨 |
| 列表 | 行内按钮通常悬停显示，带 Tooltip | 保持列表干净，同时说明按钮用途 |
| 键盘 | 上下、翻页、首尾导航 | 全程不依赖鼠标 |
| 键盘 | Enter 执行默认动作，修饰键提供其他打开方式 | 沿用侧边打开、固定等原版规则 |
| 键盘 | 支持后台接受，但需要 Provider 启用 | 适合连续打开多个资源 |
| 键盘 | Esc 通常关闭；返回按钮负责上一步 | 不随意把 Esc 改成逐层返回 |
| 焦点 | 打开与后台打开区分是否转移焦点 | 用户能选择继续搜索还是进入目标编辑器 |
| 焦点 | 是否失焦关闭可配置 | 遵守原版设置，特殊流程再明确处理 |
| 焦点 | 取消定位预览时，可恢复之前的位置 | 浏览符号或搜索命中后能回到原处 |
| 加载 | 浮层支持忙碌状态 | 查询期间明确显示正在查找 |
| 加载 | 快速结果先出现，慢结果随后补入 | 最近编辑器不必等待后端搜索 |
| 加载 | 查询改变时取消或废弃旧请求 | 防止旧结果突然覆盖新输入 |
| 加载 | 可以保留滚动位置和活动项 | 结果更新时减少跳动，避免误选 |
| 记忆 | 最近资源、最近命令分别记录 | “最近打开”和“最近执行”各有用途 |
| 记忆 | 是否保留上次输入可配置 | 沿用原版输入记忆习惯 |
| 记忆 | 历史记录可以移除 | 明确区别于关闭 Tab、删除会话 |

原版 Quick Input／Quick Access 负责输入焦点、键盘、按钮、提示、忙碌态及位置保持；项目贡献负责提供正确候选、命中信息与动作。对后台接受、定位预览恢复和输入记忆等行为按实际 Provider 的能力及配置接入，不复制另一套控件或键盘状态机。

### 当前源码与能力边界

下表是接入点盘点，不替代 tasks.md 中各切片的实施与验收证据。2026-11-03 已有文件 Search Provider、名称及部分正文查询和前端多工作区分支；两级后端合同是本次扩展的目标，尚未因这些局部实现而完成。

| 当前接入点 | 已有能力 | 尚未接通或不能假定的部分 |
|---|---|---|
| `src/clients/workbench/upstream/.../quickinput` 与项目 `search/quickAccess.ts` | 原 Provider 路由、前缀、生命周期及项目默认／专用模式接入 | UI Provider 不是后端 Search Provider；不能由入口已注册推断所有领域或运行面已完成 |
| `upstream/.../search/browser/anythingQuickAccess.ts` 与项目 Opener 贡献 | 文件／历史候选、模糊评分、层级 URI 粘贴、侧边与后台打开；项目规范 URI 已有解析及 Session／Thread／文本文件打开切片 | 已实现范围以对应任务证据为准，不能由部分 URI 打开推断所有资源 kind、定位或动作已接通 |
| `src/clients/workbench/src/threadInput.ts`、`thread.contribution.ts` | 精确 Thread 编辑器、稳定身份序列化、分屏副本展示态 | 完成范围以对应切片证据为准，不能把临时连接路由视为稳定业务身份 |
| `workspaceFiles.ts`、`changeFiles.ts`、`fileOpenMenu.ts` 与 `search/searchService.ts` | 后端文本读写、revision 校验、只读 Diff、真实文件根及 VS Code file／text Provider | 文件搜索已有接线，仍不能据此认为其它领域或全部文件入口已通过验收；通用资源不都支持当前 workspace 文件接口 |
| `filesystem.ts`、`startWorkbench.ts` | 默认 `workbench:/Workspace` 的 IndexedDB 文件容器 | 它不是当前后端业务工作区根；原版工作区搜索范围和业务工作区范围不能混同 |
| `app/api/session_navigation.py`、`session_navigation/read_service.py`、`thread_search.py` | catalog 名称／ID／逻辑路径模糊召回、独立 Thread 名称查询、预算和版本绑定续页 | 各自仍是领域查询；尚未形成跨 owner Provider 注册及能力合同，folder 结果不能当作 Session 打开 |
| `src/clients/web/src/gatewayApi.ts`、`useSessionResourceExplorer.ts` | 既有跨工作区搜索调用、取消与防抖 | 当前入口是会话资源导航中的搜索，不是原版 Ctrl+P；不应复制整套 React 资源树作为 Provider |
| `app/gateway/control/catalog_search.py` | 从公开 API 同步 catalog 投影，在 Gateway 匹配、排序并汇总跨工作区状态 | 当前全范围搜索没有显式目标列表，不能直接用于 A 或 A+B；镜像不是权威目录，须并入统一搜索合同，不能另建快速访问索引 |
| `app/api/resources.py`、`resource_navigation.py` | describe／list／search／read，path／fuzzy／glob／content、显式文本选项、精确坐标及有界续页 | 接入统一 Provider 时复用这一查询 owner；前端取消不代表所有后端工作必然即时停止 |
| `search/quickAccessBranches.ts`、`quickAccessQuery.ts`、`quickAccessPicks.ts` | 前端按工作区／类别分支，有界并发、独立状态与 fuzzy 排序 | 跨运行目标协调仍依赖浏览器，工具／脚本不能复用；远端队列与聚合须迁入 Gateway，前端保留原版交互评分／高亮、展示与本地模型／历史处理 |
| `app/core/resource_uri.py` 与统一资源寻址 owner | 严格 grammar、资源目标与稳定身份分离、归属与解析规则 | 不得加 query／fragment，不得由前端新增 kind、别名或第二个 parser |
| `agent.contribution.ts`、`changeFiles.ts` | 已有面板打开命令、打开完整补丁 Action | 还没有覆盖所有领域操作的统一命令接入；不能声称项目完全没有命令贡献 |
| `src/clients/web/src/components/composer` 与既有业务 hook | 草稿、附件、会话／任务管理 API 与业务状态流 | 原版 Chat 的附加资源动作不自动接到 React Composer，任意资源 URI 也不能直接冒充上传附件 |

## Goals / Non-Goals

**Goals:** 让不同入口共享同一目标与业务动作语义；利用项目统一资源寻址，让多工作区、多 Thread 的快速访问保持精确归属；最大限度保留原版交互机制。

**Non-Goals:** 重写上游浮层、建立通用业务对象平台、复制领域状态机、把全部资源伪装成文本文件、扩展 Electron 原生能力、承诺拼音或模型语义搜索。本轮不指定实施排期，不写逐字段接口签名或预建代码目录；下文固定职责、必需数据语义与前缀，避免高影响决策留到编码时猜测。

## Decisions

### 两级后端搜索与唯一联邦协调

```text
Web／Electron Quick Access、SearchView／Search Editor、Agent 搜索工具、脚本
                         │ 显式范围、类别和查询
                         ▼
Gateway 联邦 SearchService
  控制面工作区发现 → 解析目标集合 → 有界调度 → 增量汇总／查询排序
                 │                              │
                 ▼                              ▼
工作区 A SearchService                    工作区 B SearchService
       │ 具名、轻量 Search Provider 合同           │
       └─ catalog／Thread／文件／历史／请求／日志各原 owner
```

选择后端两级结构而非长期保留前端联邦：跨工作区查询已是 Agent 工具和脚本的产品能力，不能要求存在浏览器、已打开标签或全局活动工作区。Gateway 拥有目标登记、连接和路由事实，适合管理跨运行目标的查询；工作区拥有领域数据及其查询语义。此处明确扩展 Gateway 的查询编排职责，不将其局限为单请求代理，也不让它承担 Agent 业务计算。没有第三层全局索引或通用对象平台。

联邦 API 位于 `/api/gateway/*`，工作区查询与能力 API 位于 `/api/v1/*`。多工作区发现、排队、调度、分支重试和结果整合只有 Gateway 一个 owner；前端、工具、脚本不再各自遍历目标发同义查询。单工作区领域接口仍供原 owner／原文件 Provider 使用同一实现，经 Gateway 显式路由，不为联邦另写匹配器；联邦只含一个目标时也复用相同调度。文件的跨工作区查询由现有 `ISearchService` 集成转交联邦入口，仍保留原版 dirty model、原查询选项和根过滤；不另建第二套文件搜索服务。

#### 显式范围、全部与相关工作区

| 范围表达 | Gateway 行为 | 不允许的隐式行为 |
|---|---|---|
| 发起工作区 | UI 打开前冻结 owner；工具使用调用上下文明确携带的 owner | 从稍后活动态或上次查询猜目标 |
| 指定集合 | 校验当前 Gateway 下可访问的非空稳定 ID 集合，去重后固定 | 丢弃未知／越权 ID 后悄悄搜剩余目标，或增加同名工作区 |
| 当前 Gateway 全部工作区 | 只有显式 all 才从登记信息解析本次集合；返回解析时间、具体集合及顺序 | 省略参数、空集合或无命中解释为 all；隐式遍历所有已知 Gateway |
| 相关工作区 | 独立查询 Gateway 自有名称、ID、导航归属等元数据；调用者以发现的明确集合提交领域查询 | 从内容无命中推断扩大范围；以工作区名称筛掉已明确选择的内容查询目标 |

范围在首次分发前冻结，当前查询及续页使用同一集合和身份边界。新增登记不加入在途 all；目标被删除、不可达或归属变化，保留原分支并明确不可用／失效，不改投替代目标。新的独立 all 查询重新解析集合。目标数或响应预算达到服务上限时明确限制并要求缩小范围或继续，不静默只取 ready／前 N 个而标为全部。选择停止或断开的目标不自动连接、启动或激活它；镜像与在线读取分别表达实际来源。

相关工作区发现和内容查询是可组合的两步，不是每次请求的必经串行流程。显式全范围会话树／正文查询直接向完整选定集合分发。控制面元数据发现不读取工作区正文；Agent 可以在用户明确要求“相关工作区”的同次授权内消费发现结果，但查询需携带解析后的 ID 集合并记录实际范围，不持有可自动扩张的隐式 related 状态。

工作区名称发现、会话树分页浏览、Thread 名称检索与正文搜索是明确的不同操作；树浏览复用原 catalog 的有界列表／breadcrumb，不把空查询自动解释为下载所有正文或全量目录。跨工作区树结果保留各自根、folder／Session 类型、node ID、父节点及 breadcrumb，不能按同名 folder 合并父链或把导航节点变成 Thread；默认 Ctrl+P 空输入仍只展示最近打开。远端 Gateway 挂载继续沿既有登记投影与路由租约转发到精确目标，不递归枚举其全部邻接 Gateway 或绕过稳定归属校验。

#### 工作区轻量 Search Provider 合同

借鉴固定上游 `ISearchService.registerSearchResultProvider` 的注册／调度、`ISearchResultProvider` 的取消／结果，以及 `IWorkspaceSymbolProvider` 的查询与按需 resolve 分离。VS Code 文件合同的 `IFileMatch`、目录根和行列不是消息／日志的通用模型；proposed `FileSearchProvider2`／`TextSearchProvider2` 只作设计参考，不增加扩展宿主依赖。

| 合同部分 | 共同语义 | 保留给原 owner 的部分 |
|---|---|---|
| 注册及能力 | 明确 provider ID、类别、名称／正文模式、支持的查询选项、位置类型与合同版本；重复注册拒绝 | 查询算法、索引技术、存储结构；不要求每张表实现 Searchable |
| 请求 | 精确工作区、模式、查询选项、结果限制、剩余总期限、取消和原 cursor | 各类别 typed 筛选及自身更短的扫描／字节／时间预算 |
| 命中 | provider／类别、独立稳定身份、实际命中字段、摘要和动作能力 | 领域 typed payload、精确定位、revision 及可引用资源 |
| 页与状态 | 完成／受限原因／失败／取消、覆盖／新鲜度和原 cursor；缺能力不等于零命中 | owner 原分页、快照和错误类型，不伪造统一 revision |
| resolve／读取 | 需要时交原 owner 校验身份、版本和精确位置 | 原 opener、around、日志详情及文件读取；无需强制新增同义 resolve 接口 |

工作区 SearchService 只注册并调用原查询 owner；已有领域接口与新分派入口消费同一核心查询，不套一层返回空成功的兼容适配器。公共 typed 信封表达生命周期，命中使用明确的类别联合，不用任意字典或全部伪装为文件。能力可查询且不触发模型、执行恢复或正文扫描；未知类别／不支持的选项明确归属到分支错误。能力随目标实际版本返回，不预先枚举未选工作区。

注册键按工作区服务内的“领域类别＋操作／模式”选择唯一 owner，provider ID 标识实现及诊断归属；不能只有 ID 却让调用方猜该选谁。文件内部仍由 VS Code 的 scheme＋file／text 类型路由，后端不把公开 `boxteam` scheme 当作所有业务类别的唯一分派键。名称、正文和显式树浏览是不同操作，浏览调用原导航 owner，不强迫所有 Provider 都实现树或文本搜索。一个 owner 可声明多个操作；类别映射冲突在装配时明确失败，不靠注册顺序覆盖。相同操作确需多个明确来源时形成具名分支，而非隐式优先级或 fallback。

Provider 由现有工作区服务装配／依赖注入持有，注册返回可释放句柄。服务关闭时先禁止新准入，再取消并等待原 owner 安全结算，释放监听及可选缓存；注销／版本变化使本次能力代际失效，在途未完成分支明确不可用，不能转投另一个 Provider 或晚到后再发布。当前不新增插件热加载体系或永久等待注册的队列；请求中缺少所需 Provider 立即返回明确不支持。能力合同版本不匹配在正文扫描前归属到该分支，不能静默按旧选项执行。

#### 请求、搜索会话与缓存的独立生命周期

参考原 `FileSearchManager` 的 session／`clearCache` 和 Quick Access 的文件查询缓存，但不把它们等同于持久索引或分页快照：

| 生命周期 | 用途与释放 |
|---|---|
| 一次读取请求 | 有独立 request_id、取消和总期限；续页／重试是新读取，各自生成 request_id，单次 fan-out 全链保持同一值 |
| 同次交互的连续输入 | 可共享 owner 明确支持的短期召回缓存；每次输入仍有新查询代际和取消，旧结果不得覆盖新输入 |
| 可选查询缓存 | 以 owner／Provider 版本／身份及范围／选项隔离；可被淘汰，清理不删除业务数据，cache miss 按原有界查询执行，不是失败回退到旧查询器 |
| owner cursor／数据版本 | 用于定位续页；不能用缓存键代替，进程缓存被清理不应使本来持久有效的原 cursor 失效 |
| catalog 镜像／领域索引 | 仍由既有同步／领域 owner 管理自己的建立、更新、新鲜度及失效，不随一个 picker 关闭而删除 |

原版调用方提供的 cacheKey、delegate 和释放合同继续沿原服务执行；未持有缓存的 Provider 可明确实现无缓存，不虚构共享缓存服务。缓存资源释放与请求取消分离，不因取消一个输入清掉其它独立编辑器的查询。关闭调用方、身份／范围变化和 Provider 释放时撤销对应引用；禁止无界积累 session／缓存条目。原版可预热候选的行为在本项目仍受已选范围及有界预算约束，不因需要缓存枚举全部工作区或正文。

预热完成只表示本次有界读取结束，不表示候选全集完整。达到硬上限或 owner 扫描限制时，缓存须保留不完整／受限状态与合法续点；不能沿上游单一 Loaded 状态把截断候选当作完整集合。后续具体输入不能仅过滤不完整缓存后宣称完整无匹配，须交同一 owner 有界查询／续查，或明确报告受限并提供继续。唯一匹配在预热上限之外的场景必须可辨别此错误；不另起无界预热或扩大范围。

#### 联邦期限、渐进结果和续页

搜索核心只定义“查询＋取消＋进度／结果页＋最终完成”的 typed 合同，不导入 SSE、HTTP 或 UI 对象。Gateway 网络传输选择复用现有请求级 SSE，承载范围确认、分支状态、命中页和结束；它是核心合同的唯一当前流式编码，不要求各领域 Provider 自行实现 SSE，也不并行新建另一套搜索引擎。前端、工具与脚本消费同一查询合同，工具可在预算内收集为最终结构化结果。不新建跨请求任务数据库、不向全局事件总线广播搜索结果，也不依赖浏览器订阅存在才能运行。

从调用方到 Gateway 排队及工作区执行共用同一剩余总期限；每层只能收紧，不重置预算。取消先撤销未分发任务并中止在途网络，原 owner 在安全检查点停止，阻塞 worker 仍按原合同结算。每次用户重试是明确的新读请求，默认只重试失败分支；不能借重试偷换范围或重放管理动作。查询响应头、响应信封及向下转发沿用同一 `request_id`，分支用工作区／provider／类别区分，不另造请求 ID。

每个分支保留原 cursor、数据版本和完成状态。联邦可把这些作为类型化续查描述集合返回，但不创建掩盖分支状态的全局 cursor、跨库 revision 或精确总数。续查固定已解析范围、查询选项、身份和指定分支，原 cursor 由 owner 核验；不依赖 Gateway 进程内搜索会话，重启后在原版本有效时仍可提交。一个分支 cursor 失效，只撤下其过期页并明确重查，其它分支仍有效；不是跨工作区同一时刻的快照。

分支事件按请求内单调顺序标识，顺序只表达传输次序，不是排名或业务版本。范围确认先于命中，每个分支只有一个终态，整次结束在全部已选分支终态后且只发送一次；结束携带逐分支状态与覆盖汇总。发送层不得把同一批同时当作新增 progress 和最终新增结果重复消费；原 `ISearchService` 的最终汇总与增量进度按同一稳定键合并。正常完成、结果／扫描／字节受限、用户取消、期限失败和传输断开分别处理；取消后迟到 progress 不再发布，任何终态后无新命中。仅未受限且全部分支完整成功可显示“完整无匹配”。

首批有效命中尽快刷新，后续按有界条数／字节／等待时间批量发送，分支结束前刷新最后一批，不逐命中无限排队。慢消费者的缓冲必须有上限，不能以生产者已结束为由无限驻留内存；达到上限则等待可写、取消或期限结算，必要时明确结果交付受限，不能丢包后称成功。Gateway 不提前截断 owner 页再沿页尾 cursor 跳过未交付命中：分发前分配本页结果配额，协调器保留有界整页或 owner 提供合法续点；无法交付的页明确重查／重放该页，不能产生漏项 cursor。

#### 数量、范围与诊断的可执行合同

| 项目 | 计量与边界 |
|---|---|
| 名称／树页 | 按独立资源／节点数计，重复根或同页同身份不重复消耗展示配额；不同 Session 和 main Thread 仍是不同类别 |
| 正文页 | 按实际命中区间计，不按文件数、网络包数或预览行数计；上下文行及纯状态不计命中，仍计响应字节 |
| 联邦一次请求 | 限制工作区／分支数、并发、排队、接收／输出条数和字节；每个 owner 原扫描／字节／期限继续独立收紧。预算未用完不表示分支已完整覆盖 |
| 分配与继续 | 对冻结顺序分支有界公平准入，分发前预留页配额；暂未调度的分支保留明确未完成与初始续查描述，不以零匹配消失。后续明确请求取得新总预算与期限，仍绑定原范围和 owner cursor |
| 边界值 | 每层说明默认、合法范围及单位；规范化业务输出配额的 0 表示接受零条输出，不代表无限。上游 `IFileQuery` 缓存预热等调用的 0 哨兵按原调用合同显式转换为受服务硬上限约束的 owner 查询，不直接透传为业务零配额，也不无界预热。原版 exists 是显式探测操作；非法值、正则或选项在空目标／无命中时仍报错 |

原版 `exists` 需要单独投影为“发现一个则可停止／未完成”，不把其借用的 `limitHit` 直接当作完整正文截断。`onlyOpenEditors`、额外资源、preview 行／列限制、maxFileSize、编码及符号链接策略均由文件 Provider 明确映射到原文件 owner；未支持选项在扫描前明确拒绝。查询级和各根局部 include／exclude 的关系保留原语义；重叠根不重复扫描同一资源，冲突局部选项不能被去重静默抹掉。不能以某字段有声明就声称端到端支持。

错误使用可判别代码、原 owner 详细原因、是否可重试、request_id 和明确分支，而不是让 UI／工具解析自然语言。至少区分无效查询／正则／glob／编码、不支持能力／版本、失效 cursor／revision、目标不可达／身份失效、期限与内部读取错误；原 owner 错误不被宽泛包装吞掉。信息／警告与错误分别表达；普通受限状态和用户取消不是服务异常，不生成虚假的零结果成功。诊断只作为文本展示，不从搜索结果执行任意命令链接。记录既有本地诊断所需的排队／Provider 耗时、扫描／返回量和缓存来源，便于辨认冷启动和慢分支，不新增遥测后台或持久业务数据库。

#### 排序、身份、URI 和缓存归属

| 对象 | 权威与处理 |
|---|---|
| 工作区导航顺序 | Gateway 原 registry／navigation store 持久化，前端展示；搜索排名不回写导航顺序 |
| 会话树顺序 | 工作区 catalog 权威，名称搜索与分页不改变树的持久顺序 |
| 名称搜索排名 | Gateway 提供明确查询排序：默认稳定类别／范围／owner 顺序；显式相关性模式仅在可比较合同成立时合并，并公布排序规则版本。原 Quick Access 对已返回候选继续使用上游 fuzzy scorer／MRU 做交互评分与高亮，不搬到 Gateway 重写一份上游算法 |
| 正文搜索排名 | 默认按类别、范围固定顺序及 owner 原顺序组织；不同 provider 的分数未经共同合同不作全局相关性排名 |
| 最近打开及本地模型 | 前端沿原 Workbench 历史设置排序；dirty model 仅覆盖同范围同文件的后端命中，不保存、不上传其它工作区草稿；工具得到已提交后端事实，差异明确 |
| 索引身份 | 原 owner 稳定 ID 及 Gateway／workspace 归属；文件继续用原根／资源位置合同，显示名和临时 route 不作主键 |
| 可打开／可引用命中 | 默认由原寻址 owner 提供规范 URI，并携带独立稳定身份；URI 的缺口先登记合同，不由前端拼协议 |
| 命中位置和版本 | 行列、消息／事件位置、revision 独立表达；可用资源 URI 加位置时不为每个命中再造地址，固定版本仍须明确选择或遵守原领域合同 |

默认 Ctrl+P 的“最近 → 会话 → Thread → 文件”组序不变，Gateway 负责远端分支调度及明确查询排序，前端复用原版对已返回候选的 fuzzy 比较器、高亮、MRU 和活动项保持；交互评分不是第二个联邦查询器，不要求工具复刻浏览器个性化排名。不能在前端再次分发远端搜索或拉全量来补排序。分页后的名次只代表已返回结果，可有后续更优候选，不声称已求得全域 top K。搜索去重按类别及 owner 身份，正文还包含位置／版本；URI 用于打开与引用，不是所有索引记录必须预建的主键。命令、进度、分隔项及纯导航动作不强制 URI。

URI 等价由原寻址／文件 owner 决定，再通过唯一内部投影进入 `IUriIdentityService`，不以 `URI.toString()` 或全局 casefold 擅自决定 Windows／Linux／SSH 目标大小写。重叠根、额外文件、已打开模型和远端结果使用相同比较键；路径别名／符号链接只沿原 owner 已公布的 canonical 规则，不自行用显示名或 realpath 合并。不能把两个不同后端资源因显示路径相同而去重。

本地覆盖集合包含已搜索的适用打开模型，即使模型零命中也屏蔽同文件的已提交后端命中；本地命中及其上下文也占用文件查询展示上限，剩余配额才交给远端，不将模型全文或其它工作区草稿发送给 Gateway。普通文件的负命中覆盖不是取消所有远端分支，未覆盖文件继续沿原预算查询。模型版本在查询过程中改变时，沿原模型／查询代际刷新或废弃过期定位，不能拿旧本地位置套到新文本上。

跨工作区文件查询在同一代际冻结本地覆盖集合，按稳定工作区及原文件 owner 的 canonical 身份分区；向文件分支只传所选范围内的覆盖资源身份，不传模型正文。本地模型仍由原 `ISearchService` 搜索；Gateway 转交文件 owner 在匹配输出计量和分页前排除这些已覆盖资源，不在客户端丢弃受限页中的旧命中后冒充完整页。排除条件参与查询缓存及 cursor 语义绑定，模型变化需废弃旧代际或重新查询，不沿改变后的覆盖集合使用旧 cursor。这是文件查询选项，不强制其它领域实现本地模型覆盖。验收同时选择 A+B：A 未保存模型删去查询词，B 同路径仍含词，A 另一文件也含词且结果配额很小；A 旧命中不能消耗远端输出额度或推进跳过其它命中，B 与 A 其它文件均可按合法续页取得。无浏览器工具不携带本地覆盖集合，继续查询已提交事实。

保留现有 Gateway catalog 镜像作为公开目录的派生缓存，不引入新索引库，也不把镜像当作权威或用来回答任意正文查询。在线读取转交工作区名称 Provider；明确使用已有镜像时，匹配／字段语义必须与同版本 owner 合同一致，返回来源、更新时间和 stale 状态，实际操作再经原 owner 核验。目录镜像的匹配规则由同一领域 owner 维护共享实现，不继续由 Gateway 单独演化；搜索不触发扩大既有后台同步范围。缓存失效仍归原同步 owner。

#### 原入口迁移与无浏览器消费者

清点 Gateway catalog 搜索接口、旧 React 资源导航、WorkBench 分支代码、原文件 SearchService 及所有间接调用／测试，沿原接口演进为同一联邦合同。迁移所有消费者后物理移除前端跨工作区队列、同义聚合 API 与独立匹配分支，不保留“旧快搜／新联邦”长期双轨；已有 owner 单工作区接口是领域能力，不因联邦而复制或废除其非搜索业务用途。

Agent 搜索工具与脚本显式接收当前／指定集合／all 的同一范围语义，工作区上下文不足时要求指定，不猜全局活动项。工具先发现工作区名称，再查询选中工作区会话树／Thread，或直接执行用户明确的 all；结果保留规范引用、分支受限／错误和实际范围。查询只读，不把结果自动附加到其它 Thread、不发新 turn；工具调用的原授权和既有执行记录保持不变。产品验收必须包含关闭浏览器后的真实 Gateway 工具／脚本调用。

#### 2026-11-03：两级搜索的迁移与删除清单（16.1）

全仓核对了 URL、导出函数、Protocol、调用侧服务与测试；脚本目录 `scripts/`、`tools/` 当前没有在线领域搜索消费者，已有历史资产生成／旧库导入脚本只引用 catalog 存储 owner，不属于联邦调用方。后续脚本入口直接消费同一 Gateway 合同，不保留脚本内 fan-out。

| 现链路与消费者 | 迁移后唯一职责与删除边界 | 受影响验收 |
|---|---|---|
| `gateway/control/router.py` → `GatewaySessionCatalogSearchService.search` → 镜像；`web/gatewayApi.ts:searchGatewaySessionCatalog` → `useSessionResourceExplorer` | 旧导航迁联邦后删除 `/api/gateway/session-catalog/search`、旧聚合 DTO／客户端与 `.search`；保留后台 `_sync_all/_sync_workspace`、runtime config／health、持久镜像及原导航 folder 发现。在线走工作区 Provider，离线镜像仅共用原 catalog 匹配 | `test_session_catalog_search.py`、`control/test_catalog_search.py`、Gateway schema／auth／config／lifespan 合同、`browser_view_sessions.mjs` |
| Workbench `quickAccess.ts` → `quickAccessBranches.ts` → `quickAccessQuery.ts` → catalog／Thread／history API；`quickAccessPicks.ts` 排名 | 逐领域迁 Gateway；迁齐后删除前端 Limiter、跨目标路由／重试／期限队列与同义远端排名。保留 Quick Input、明确 scope、组序、高亮、MRU、接受与活动身份保持 | `default_contract`、`default_scope`、`query_lifecycle`、`message_content`、catalog targets／pagination／source documents E2E |
| 原 `ISearchService` singleton → `searchService.ts` → `fileProvider.ts`；`target.ts:loadSearchScope` 解析根 | 跨工作区文件调度交 Gateway；保留唯一 file／text Provider、领域 navigation 查询、原选项、根／extras／位置、单根 cursor、dirty model 同文件覆盖。明确根的元数据解析与原调用方 cache/delegate 不是另一套搜索队列，不机械删除 | `search_service`、`scoped_query`、`text_search`、`callers/original.test.mjs`、文件 Provider 合同 |
| `agents/tools/session_history.py:search_context`、`session_target_resolver.py` → `GatewayContextQueryService.search_gateway_context` → transport／`GatewaySessionContextClient` → federation relay → `workspace_port.py` | 调用侧现有 Semaphore／全工作区 gather、跨库 revision／全局 offset 聚合必须迁到 Gateway 的唯一分支合同并物理删除；保留 inventory 读取、精确单资源 read/search、现有 federation grant、传输与本地 `SessionContextQueryService` 的来源／预算。effective_context、canonical 消息、目录名是不同领域，不能用同一正文扫描器替换 | `test_gateway_context_query_service`、工具／target resolver、Gateway routing／catalog errors、transport／protocol errors、context publication、Docker offline 场景 |
| 原工作区 `/session-catalog/search`、`/session-threads/search`、`/sessions/history/search`、`/resources/navigation`、`context.py` | 作为原领域 owner 入口保留并与 Provider 调用同一核心实现；树浏览用原 list/breadcrumb，不能把空查询作为全量搜索。模型工具发现与 Web Search 属于不同领域，不迁入工作区资源搜索 | 各原 owner 的分页／取消／期限／归属合同及单入口对联邦的语义一致性 |

本清单是迁移范围，尚未表示共享信封、调度或调用方已经实现。第 16 组其它验收须分别提供真实入口证据；现有后台同步仍按原范围运行。

### 一个交互 capability，引用现有领域 owner

新增 `workbench-quick-access`，由本 change 维护查找、辨认和动作选择的可观察合同。统一资源寻址的 grammar／scope／kind／ResourceIdentity 仍只引用 `add-unified-virtual-resource-addressing`；文件、会话目录、上下文、任务和附件保留各自合同。

不复用已经完成的工作台迁移 change，以免改变已完成验收的范围；不塞入 URI change，因为 UI 搜索与动作不是 URI grammar 的职责；暂不拆多个 change，因为它们共同构成一条交互链，拆分容易重复目标解析和结果模型。设计按职责拆分，不提前创建一层层抽象或实现目录。

### 项目贡献接入原版服务

```text
领域查询 API ──候选──→ Quick Access／Quick Input
                             │
                  明确目标 + 用户选择的动作
                             │
           ┌─────────────────┼──────────────────┐
           ↓                 ↓                  ↓
     打开服务／编辑器     引用到指定 Composer    领域命令／后端 API
```

#### 唯一默认 Provider，复用原版基础设施

固定上游为 `fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca`。`platform/quickinput/common/quickAccess.ts` 的 Registry 只有一个空前缀槽位；`AnythingQuickAccessProvider` 的普通混合路径将文件、历史、符号组织在 private 方法中，现有 `fileSearchDelegate` 只在 `filesOnly` 路径有效，`additionPicks` 又是已弃用的静态插入参数。因此不把“注册几个独立 Provider”当成默认融合，也不运行上游全范围搜索再过滤。

选择在项目 contribution 中以原 `PickerQuickAccessProvider` 实现唯一业务默认 Provider。复用原 Controller、Picker 生命周期、`prepareQuery`／`scoreItemFuzzy`／比较器、HistoryService、Opener、EditorService、EditorResolver、组与 `PickerEditorState`；只负责领域查询、范围、候选及动作。相较继承 Anything 后覆盖几乎全部私有查询路径，这个扩展面更明确；不复制上游 UI、键盘状态机或整份 Anything 源码。

原 Registry 描述符的 `ctor`／`prefix` 是 readonly，不能运行时改写。接入复用现有 `src/clients/workbench/scripts/sourceTransform.mjs`，仅在上游原贡献的登记表达式处注入项目描述符选择函数：输入／返回仍为原 `IQuickAccessProviderDescriptor`，以新对象选择项目 ctor，原 register 只执行一次。空前缀与 `%` 各有唯一贡献，保留原快捷命令 ID、prefix、contextKey、帮助与顺序；其它前缀沿原登记。该 transform 已由 Vite、依赖预构建和生产构建共用，Web／Electron 使用同一转换，不新建第二条改写管线，不复制整份上游贡献文件，也不改写固定子模块。对固定 commit 和每个转换锚点核验，锚点必须恰好一次，失配立即失败；生成的贡献代码也必须进入真实类型检查。禁止 readonly 类型逃逸、运行时 mutation、prototype 变异、测试用 Registry.clear 或重复注册同前缀抢占。

默认入口的分支也须完整接入：层级 URI 交 Opener；普通名称由领域 owner 查询；文件名追加行列使用原 `extractRangeFromFilter` 和 selection；文件候选追加 `@` 使用原 `GotoSymbolQuickAccessProvider.getSymbolPicks` 与预览恢复服务，名称本身含 `@` 时沿原转义／识别规则；quickNavigate 只导航相应历史，不发领域全量查询。只有业务资源支持的分支才提供相关动作，不对 Thread 名称套用文本行列解析。原 `filesOnly`、只选择调用方继续得到其声明的资源类别，不因替换默认 Provider 混入 Session。原运行参数不是可以删除的旧接口：`emptyFileEditorSearch` 实际依赖 `filesOnly`、`fileSearchDelegate`、`includeOpenToSideButton` 及自身接受／焦点恢复；必须执行调用方提供的 delegate，保持其范围、cacheKey、lifetime 和清理，不能只对项目普通结果过滤。`chatContextActions` 仍使用 `filter`、`additionPicks`、`handleAccept` 及 enabledProviderPrefixes：补充项按原评分／高亮进入列表，过滤按调用合同应用，接受交还调用方并保留 background 标志及关闭时机，异步二级选择取消后返回原 picker。`additionPicks` 虽标 deprecated 仍有生产调用，不视为死代码；若后续改参数，必须先迁齐这些消费者再移除。原生 Chat 的显式调用继续服务其原 widget，与项目 Composer 的引用入口分别尊重各自接收者，不因默认 Provider 替换而互相投递。

原 `PickerEditorState.restore` 与浮层 hide/dispose 存在 await 交错：本次恢复在等待关闭 transient editor 前捕获并消费状态，之后只使用局部捕获，避免释放后读取空状态，也避免旧恢复清空下一次预览。此修正同样通过唯一 sourceTransform 接入，保留原服务和视图状态，不复制恢复器。普通空输入只显示历史；`filesOnly` 和 delegate 属于原显式文件调用，空查询仍按其合同交给原查询／delegate，不套用默认混合入口的空输入规则。

Command Center 的 `workbench.action.quickOpenWithModes` 以 `includeHelp: true`、`from: commandCenter` 调用默认 Provider，这是普通 `?` 之外的真实入口。空输入时将原 Registry 中当前上下文可用、具有 commandCenterOrder 的 help entries 作为快速模式候选，项目已启用入口也提供相应 help metadata；沿用顺序、标签、快捷键及无障碍描述，不等待后端资源查询。非空输入不把这些帮助当作资源结果。选择模式后保留原 provider options／preserveValue 语义和本次发起上下文，使用 Registry 的原前缀切换；返回默认模式时关闭 includeHelp 的递归。范围和接收者沿同一次交互传递，不因模式重开重新取后来的焦点；原生 Quick Chat 等附加候选仅在其真实服务可用时沿原行为提供。

发起归属由原 QuickAccessController 在创建和显示浮层前同步捕获，写入本次 typed provider options；原 Controller 的输入前缀转接继续显式传递该 options。原 HelpQuickAccess 的确认与精确前缀两个 show 调用同样传递本次 options，返回空前缀时只关闭本次 includeHelp。接入均留在唯一 sourceTransform，不替换原帮助列表或交互。新发起的独立命令不读取上次浮层的参数或接受回调；没有有效归属的发起错误随本次转接保留，不因后续焦点改变而猜新目标。

#### 文件搜索根与原版搜索视图

文件检索注册到同一 `ISearchService` 的 file／text Provider，原文件操作 owner 负责真实后端查询。搜索根取后端公布的规范目录身份及其唯一内部编辑器投影；目录 `stat`／describe 必须真实，不能以固定成功骗过原 SearchService 的存在性检查。根下文件的 URI 拼接、父子判断和相对路径都须往返核验；不能把目前在 query 中保存整条原文件 URI 的叶子投影直接当成可 `joinPath` 的目录。继续使用同一文件投影函数，不新增搜索专用文件 scheme。

文件内部投影使用 `boxteam-file://<gatewayId>/<workspaceId>/<编码后的规范根>/<解码子路径>`；根是不透明引用段，追加子路径只反转分段编码，公开 URI grammar 和访问权限仍由原后端 owner 校验。目录根和所有文件使用同一函数，query 不保存临时 route 或叶子定位；执行时按稳定归属重解登记。旧文件记录缺少 Gateway 归属时明确报错并保留原记录，不清历史或猜当前目标。物理 describe 在原导航 node 返回可选 stat（字节长度、修改／变更毫秒时间），只在 describe 请求提供，不让目录列表的每个节点额外膨胀；不读取正文或计算内容 hash，也不更新编辑器读入的保存基线。

`TextSearchQuickAccess` 与 `SearchView` 都从注入的 `IWorkspaceContextService` 取根，注册文件 Provider 本身不能改变它们默认使用的 `workbench:/Workspace`。选择对这些业务搜索实例使用原生 child InstantiationService，注入只读的业务范围上下文，并让 QueryBuilder 在这个 child 中实例化；不改变整个 Workbench 的工作区、存储 ID 或扩展上下文。`%` 的原描述符构造入口创建并持有该 child，再在 child 下创建原 TextSearchQuickAccess；缓存 Provider 的每次 provide 必须在读取根之前捕获新发起上下文，结束时取消本次查询，销毁时释放 child。只在旁边准备 child、仍由 root 创建原 Provider 不会生效。`%` 保留原实现；SearchView 保留原实现的业务子类，并在原 `ViewPaneContainer.createView` 的 typed 创建入口用此 child 创建，保持原 VIEW_ID、菜单和布局，不做另一套搜索页面。业务范围上下文是后端目录和本次范围的展示投影，不成为新的工作区权威。SearchView 的范围保持到显式新查询或切换范围，更新时发布原 workspace/folder 事件并先取消旧查询；普通导航焦点改变不更新它。不得让 QueryBuilder 缓存旧根后界面却显示新范围。同一 sourceTransform 在原 SearchView descriptor 选择项目子类，并在 ViewPaneContainer 的实际 createView 处仅为原搜索视图创建业务 child，其余视图沿原构造路径。固定上游允许用户将视图移动到其它容器，因此不能只替换默认 Search 容器：实际创建处的 hook 使移动后仍保持同一范围合同。只执行原登记，保留 VIEW_ID、既有视图、位置与用户自定义布局，不拆除重登记或增加第二容器。

全局仍只有一套 SearchViewModelWorkbenchService 和 ReplaceService，项目搜索服务通过原 singleton 登记机制在 Workbench 创建前成为唯一实现；登记顺序须在原 RemoteSearchService 之后，验证最终 ServiceCollection 中仅该描述符生效，不能只依赖模块导入的偶然顺序。不能在 child 另建搜索视图模型，使原菜单、替换、无障碍动作读取另一份结果。`%` 转搜索视图时，使用原 `replaceSearchModel`，同时接续查询模型内的明确范围；后续修改查询仍使用该范围，不跟随导航或其它编辑器改投。Search Editor 遵守 `search.mode`，范围按下节逐 input 保存，不只验证首次结果转交。

`#` 同样在原登记处选择原符号 Provider 的项目子类，保留原 descriptor、Picker 及生命周期。现有全局符号服务没有业务目录参数，因此带明确业务工作区或发起身份错误的查询，在调用任何全局符号 Provider 之前显示未启用或原错误；不能查询后再过滤。未绑定业务范围的原生符号调用沿原服务，文本文件内符号仍使用该精确模型。后续只有具备查询前限定完整业务根的真实符号服务，才开放对应业务工作区符号结果。

原 SearchService 的 `getOpenEditorResults` 用 `fsPath` 匹配，不能隔离不同 authority 下的同路径文件。项目唯一搜索服务继承原 RemoteSearchService，只对带明确业务根的文件正文查询重写公开 `textSearchSplitSyncAsync` 组合：本地未保存模型按完整 owner／资源身份和所选根过滤，后端命中由同一文件 Provider 按完整资源身份和显式路由提供，不能送入仅以path找浏览器handle的LocalFileSearchWorkerClient；同一文件本地模型优先，既不自动保存也不覆盖用户编辑；原生容器查询继续使用原逻辑。其它 file／text／cache 方法沿用原服务；业务范围不新增语义／AI搜索；该分支即使被原版自动语义设置调用，也必须明确不支持且不向其它根或模型发请求，不静默启用后台AI或改写用户全局配置。语言能力按实际服务提供，不能伪造结果。不能调用基类 split 后只过滤返回的 syncResults：它在返回前已向 onProgress 发出本地命中。业务分支在扫描模型和发送每次进度前就按完整 owner限定。仅在返回结果后过滤无法满足“不得查询其它工作区”的合同，查询根、`extraFileResources`、本地模型及符号来源都须在发起前限制。

搜索选项按原版 QueryBuilder 传递：正则、大小写／smart case、整词、include／exclude、ignore、仅已打开资源及结果上限。文件 owner 必须补齐对应已展示选项的语义，或明确拒绝不支持的选项，不将 substring 接口冒充等价搜索。此范围不新增批量替换业务；已有可写文件的原版替换动作若保留，必须走同一 revision/CAS 保存合同并明确冲突，不能借搜索接入绕开写入 owner。

文件导航以可选 `search_options` 表达已支持语义：case_mode、正则、整词／分隔符、多行、全部非重叠命中、include／exclude 与 ignore 模式。省略时保留敏感字面量、每行首个命中和既有固定目录排除；显式 `none` 可关闭固定排除，私有存储权限仍保持。`respect` 在目标机器解释所选根以内的 `.gitignore`／`.ignore`，复用 pathspec 的 gitignore 匹配；不在 TS 再造规则引擎。`.ignore` 优先于 `.gitignore`，同类规则由最近目录的最后匹配决定，目录剪枝发生在递归和正文读取之前。每目录只读取一次规则，累计规则正文受原 max_bytes 与总 deadline 约束；规则 stat 及缺失标记进入原 cursor 前缀，不另算正文 hash。父目录和全局规则仍明确拒绝。显式单文件与已打开模型沿上游搜索行为，不额外应用磁盘 ignore；完整 owner、根及显式 glob 约束保持。`%` 由原 QueryBuilder 消费用户的 search.useIgnoreFiles，默认开启即可搜索，关闭后沿同一文件链包含被忽略文件，不擅自修改全局设置。正则复用现有依赖中的 regex 库，编译后的模式按一次本机导航复用，每段匹配只使用剩余期限。smart case 沿原 QueryBuilder／containsUppercaseCharacter，正则转义不参与大写判定。命中 start/end 是一索引 Unicode 码点、终点不包含，片段仍为起始行的有界展示；原 owner 同时提供基于完整文本的 UTF-16 起止及片段起点列，避免从裁剪片段猜测或重复读取正文；客户端搜索 Provider 接入时负责消费明确坐标和“仅已打开”的精确目标选择，不能把后端能力直接算作完整 UI 验收。

名称召回在原 `ResourceNavigationRequest.search_mode` 增加显式 `fuzzy`，按所选根内解码相对文件路径进行大小写折叠的顺序字符匹配，只返回文件，不读取正文。原 `path` 大小写敏感包含查询保持不变。结果沿原稳定遍历及 cursor 分页，前端负责组内评分；扫描节点、结果、响应字节和总 deadline 仍沿原 owner，不构造持久文件索引。异步取消释放等待方，原生阻塞 I/O 恢复后继续核对原期限，不声称可强行中断文件系统。

不修改固定 upstream 子模块字节；上述贡献与搜索范围接入均使用既有受校验 transform，不将 Provider 代理到其它搜索弹窗。已有文件菜单、会话树、引用入口沿相同业务动作收口，各自默认交互保留。

文件编辑器监听复用现有目标文件 watcher 和共享观察 SSE，同一稳定 Gateway／workspace／规范根仅保留一个订阅，最后一个监听者释放时取消。正常变更只通知原 FileService 核实已打开资源，连接重建或事件溢出重新核实所监听资源；监听不读取正文或推进保存基线，不再每个文件每五秒拉取全文。这与参考仓库 Copilot 直接使用 `workspace.createFileSystemWatcher` 的职责一致，文件写入仍沿原 revision／CAS。

#### Search Editor 每个输入保存自己的范围

原 `SearchConfiguration` 没有业务 scope，原 serializer 只保留 config、modelUri、dirty、matchRanges、backingUri；单独注入 child 上下文不能让关闭恢复或分屏复制保持范围。选择在原 SearchConfiguration 上增加可选、版本化的项目 scope metadata，由同一 transform 接入原类型声明并引用项目的具名类型。metadata 只表达 Gateway 稳定归属、选定工作区稳定身份及规范目录根，不保存 token、临时 endpoint 或把 route 冻结成身份；未绑定与原生容器范围须明确区分。它是这条查询的展示／恢复数据，不是新工作区事实源。

scope 跟随原 Input 的 configuration model，原 JSON serializer 的 config 字段原样保存此扩展，不另建以 modelUri 为 key 的范围缓存。原 `serializeSearchConfiguration`／`extractSearchQueryFromLines` 的唯一序列化入口同步处理版本化 scope header，使 `.code-search` 保存／另存、working-copy backup 和重启恢复走同一格式；原 `extractSearchQueryFromModel` 的固定前六行读取也须改为完整 header 读取，不能因新增 header 丢失后部选项。未知版本或损坏 metadata 明确报错，不能当作缺省范围。只含旧标准 header 的文件仍可查看已有结果，但缺少可核验业务范围时，重跑前必须让用户明确选择；不从当前导航或文件所在工作区猜它原来搜哪里。

原 Search Editor Pane 的登记表达式通过同一 transform 选择项目 `SearchEditor` 子类，保留原 Pane／Input ID、原 UI、原类型关系、命令与模型；每个 Pane 持有自己的 child 范围上下文，`setInput` 在调用原实现前先读取该 input 配置、重解当前已登记 route 并绑定根；读取模型等 await 前后核对取消 token 与 input／scope 代际，不能让已被替换的 input 晚到并覆盖绑定。`clearInput` 和新 input 废弃旧查询及迟到结果、撤销旧根绑定。根更新事件只发送给对应实例，两个组中 A、B 搜索编辑器可同时使用不同范围。身份无法核验时已有结果标明历史且禁止重跑或错误目标打开，不能使用上一 input 的范围。

仅选择 Pane 子类不改变原 SearchEditor 构造器已注入的 private readonly contextService，也不改变原 doRunSearch 使用的 InstantiationService。因此在同一受校验 transform 中，把原 doRunSearch 内构建 ITextQuery 的小段提取为具名、typed protected hook：缺省实现保持原生容器查询，项目子类按本轮捕获的 input／scope 生成 query，使用该实例 child 的 QueryBuilder 和明确目录根，并在构建前限定 extraFileResources。原方法只调用这一个 hook，不再先读取 root 的 folders 或先创建 root QueryBuilder；不复制 doRunSearch，不尝试 override private 方法或改写 readonly 注入字段。scope 未绑定、已失效及构建失败均明确反馈，原 catch 后直接 return 不能吞掉这些错误。原文件内容结果、查询参数和后续搜索模型仍由原实现处理；scope 切换和 clearInput 的取消必须作用于实际原 searchModel 操作，迟到结果检查 input／scope 代际后才可写回。生成代码的类型检查和真实查询验收须证明 query.folderQueries、extraFileResources 及实际 HTTP 目标一致，仅 child 已存在不算接入完成。

需要一起迁齐的实际数据路径是：SearchView／`%` 的 query 转 Search Editor config、openNew／openEditor 命令、factory 的 rawData／model／existingFile 三个入口、Pane 从控件重建 config、Input.copy 分屏复制、原 JSON serializer 与保存／备份的 header。每条路径保留明确 scope；工厂的 priorConfig 仅复用原设置允许的查询选项，必须剔除 scope，不能让“复用上次搜索配置”把新搜索改投旧工作区。原 copy 的 config 复制须核验 scope 被保留，复制组独立持有展示态和查询生命周期。原 `readConfigFromWidget` 逐字段创建 config，必须从当前 input 保留 scope；`doRunSearch` 的 configurationModel.updateConfig 和异步完成也须绑定该 input 与 scope 代际。`setSearchConfig` 原本只改可见控件，复用／更改范围时必须同时写回 input 配置，不能只更新控件或 child 根。原 `createEditorFromSearchResult` 的两个 rawData 分支都从传入的 SearchView query／绑定范围显式接收 scope，不重新读取活动工作区。不假定只有构造时传一次 metadata 就能覆盖后续控件重写。

新建查询由明确发起上下文绑定；复用已有搜索编辑器须核对 scope，相同才按原复用规则，范围不同默认新建，除非用户明确选择在该编辑器更改范围。范围作为原搜索界面的明确次要信息展示，使用原命令／Quick Input 提供选择，不藏在内部配置里；它不混入原文本查询输入。显式改范围先取消旧操作、清除旧命中和选择状态，再保存新 config 并查询；普通换焦点不会改范围。保存的范围重启后重新解析已登记路由，目标不可达明确报错，不自动建立连接、启动或扩大范围。结果中同显示路径文件必须仍能经完整根身份定位，不能仅靠 `.code-search` 展示路径拼到当前工作区。

### 明确目标，分离展示、引用、定位和连接

候选至少应能表达资源类别、名称、归属与可用动作。需要作为资源引用或重新打开的对象，已有规范 URI 时直接复用，确有缺口时由统一寻址 owner 正式登记；没有此类需要的命令、导航动作或瞬时状态不普遍补 URI。可用 Thread URI 加结构化位置准确表达的定位不再额外创造消息／轮次地址。身份、URI、文本 selection、Thread 视图／轮次选择、revision／hash、当前传输 route 分别按已有合同传递。本稿不是新 DTO 定义。

`boxteam-thread/file/change/source` 是原版编辑器集成的内部定位形式，不作为用户复制或跨 owner 保存的第二套业务 URI 协议。规范目标到这些编辑器输入使用唯一投影；Thread 身份取原 owner 的 `(workspace_id, session_id, thread_id)`，跨 Gateway 传输另保留其权威 Gateway 归属。重连、改名和历史重开按稳定身份重新解析当前已登记路由；编辑器匹配／历史去重不比较临时 route 或标题。重解失败明确不可用，不退回当前活动路由；多条路由无法唯一确认时要求明确选择，不自动创建连接。文件身份同样包含根及完整相对资源位置，不能仅比较显示路径。原版候选 URI 比较去重只是展示处理，不代替领域身份、权限或幂等判断。

工作区 AGENTS／Skill 来源的内部文档投影保留 Gateway／工作区身份、不透明规范引用和展示文件名，沿原资源导航读取。原 `ITextModelService` 内容 Provider 与 `TextResourceEditorModel` 管理只读正文及引用释放；不为来源伪造文件 stat 或登记普通文件 writer。统一 Opener 在建组前取得同一模型引用，以一次路由重解与读取完成核验，交原编辑器接管后释放预读引用；历史与默认搜索范围也消费该投影归属。非文本或截断内容明确拒绝作为完整文档打开。其它来源按原 owner 的实际读取能力接入，不借此绕过配置或附件领域。

原 Opener 的项目贡献将原始规范 URI 交给认证 Gateway 的 `/api/gateway/resource-addresses/parse`，由 `app.core.resource_uri` 唯一 grammar 投影规范引用、Gateway／scope／kind 和已有 Session／Thread／文件根字段。此只读入口不探测文件、不确认资源存在、不产生访问能力，也不启动目标；公开结构由 Gateway proto 描述。客户端不重复模板解析，在实际打开时重新解析已登记工作区路由，并交原 Session／Thread／文件 owner 核验；不支持的资源形态明确报告。Quick Access 传原始 URI，不先应用文件行号或符号后缀识别，位置继续作为独立 editor options；发起组和后台／侧边选项在异步解析前冻结。

同名候选显示不同归属；来源与目标固定后，即使活动工作区或焦点 Thread 改变，也不能默默改投。快捷操作针对当前上下文时，必须明确究竟使用焦点编辑器 owner、显式选中目标还是工作区控制面。

### 各资源如何接入

| 对象 | 发现与定位基础 | 打开／引用 | 管理与待补边界 |
|---|---|---|---|
| Session／folder | 权威 catalog 搜索及导航投影 | Session 由 owner 解析 main Thread；folder 展开／定位导航 | 复用既有目录命令与 outbox，区分 pending／confirmed；不扫物理目录 |
| 精确 Thread | 已打开编辑器、会话 main／child 查询、规范 Thread URI | 保留明确 child，原版 Thread 编辑器与组 | 在 Thread owner 补齐工作区级 main／durable child 的有界发现查询，不得把 child 退化成 main |
| 普通文件 | 规范根、资源导航搜索 | 文本编辑器、结构化行列、复制 URI／选为上下文 | 保存保留 revision 冲突校验；现有文件树单击仍在会话资源内预览 |
| 文件内容与代码符号 | 内容搜索已有；符号依赖对应语言／索引服务 | 命中行列定位；文本符号沿用原版语义 | 明确不支持的搜索选项，不能静默忽略；不把会话消息强塞进代码符号 |
| Skill／AGENTS／配置来源 | 已有规范资源目标与资源 owner | 依据公布能力打开文本或引用 | 浏览不等于激活来源；按原 owner 判定只读／编辑，不能通用写覆盖业务配置 |
| 附件／工具结果 | 引用附件与工具结果 owner 的导航及生命周期 | 媒体／只读／文本能力分派，不强制文本 | 继续保留独立上传身份、变体和版本合同，不绕过附件 owner |
| 变更 | Session changeset、edit／phase | 现有 Diff／patch 输入及明确版本 | 通用资源 kind 未覆盖的部分用领域身份；不把 before／after 当可写当前文件 |
| 持续任务／Goal | 工作区任务 owner、会话关联投影 | 定位原自动化详情，引用能力按原合同 | 暂停／继续／检查等直接调用原 owner；Goal 归属按既有 main Thread 合同，原版 `task ` 不自动代表它 |
| Gateway／工作区 | 既有控制面与注册信息 | 定位导航／工作区面板 | 切换、连接、启动须明确动作；选中候选本身不启动后端 |
| 终端／浏览器／MCP | 既有工作区资源及 Thread 关联 API | 聚焦具体实例或原资源视图 | 与原生 `term `／`mcpr ` 的服务映射需核对；无正式规范 URI 就不发明 URI |

### 默认结果排序与入口名称

Thread 名称发现使用原工作区 catalog 的可见快照限定 Session，由原 Session owner 在生命周期读准入内读取同一控制库事务中的 main／published child 名称。每次最多检查 64 个目录节点、512 条 Thread、返回 100 个匹配；分支调度预算为 1 秒，单次准入与同步 I/O 仍沿用原 owner 的期限和受保护 worker 结算规则，不宣称任意 I/O 都能在 1 秒强行中断。child 用稳定 ID keyset 续查，不随页数重复 OFFSET 扫描；不建立新表或正文索引。cursor 固定 Gateway／workspace、规范化查询、catalog revision 与当前控制库原插入型 Thread revision；当前分支新增 child 拒绝旧 cursor。各 Session 分支是各自读取时的快照，不假称跨库同一时刻；已完成分支后续发布的新 child 需要重新查询。完成、结果条数、扫描和时间预算分别披露；有 cursor 的空页表示尚未覆盖完成，消费者不得展示成全量无匹配。

- 空输入只显示所选范围内的“最近打开”，遵循原版历史顺序及移除动作；历史关闭时不枚举全工作区资源来填空。`edt` 系列保持原版跨组编辑器导航用途，不被默认资源搜索范围改造成另一套列表。
- 非空输入按“最近打开 → 会话 → Thread → 文件”固定组序展示；`search.quickOpen.includeHistory` 控制是否有历史组，`history.filterSortOrder` 控制其相关性／最近顺序。其它组采用原模糊评分，分数相同时以稳定名称／归属键排序；晚到结果不能以总分重排其它组或抢走活动目标。
- 同一类型、同一 owner 身份的候选只出现一次，最近记录优先占位；同一 Thread 的多个组副本在默认资源搜索合并为目标，精确选择某组使用原 `edt` 系列。Session 与 main Thread 保留不同领域对象和动作，不因打开后相同就互相删除；child 永不并入 main。正文中不同 owner／不同位置的命中不按片段相同去重。
- `search.quickOpen.includeSymbols` 开启且具备真实符号服务时，附加独立符号组，查询仍限所选根；不以全局符号 Provider 查询后过滤冒充范围限制。没有支持范围的真实符号 owner 时明确未启用，不改变前三类核心资源的要求。
- 项目专用前缀采用 `session `、`thread `、`resource `、`automation `、`workspace `、`content `，英文协议前缀与中文提示／命令名分离。帮助列出这些已启用入口；最长前缀匹配沿原版。`@:` 是 `@` 内的分类形式，URI 是粘贴形式，quickNavigate 是键盘操作，均不另注册伪 Provider。
- 默认动作：Session 打开权威 main Thread，Thread 打开精确 Thread，文件使用原文本／媒体解析器，folder 定位导航，任务打开其原详情，已有运行资源聚焦实例；暂停、继续、删除、连接／启动、引用均须明确动作。行内优先展示侧边打开及“更多动作”，其它动作通过可键盘操作的原 Quick Pick 和命令入口提供，不堆一排图标。

### Thread 大纲复用非文本编辑器机制

固定上游 `GotoSymbolQuickAccessProvider.provideWithoutTextEditor` 可调用 `IOutlineService`；项目 ThreadPane 是自定义 EditorPane，不是假装成原 `IChatWidget`。为 Thread 注册原 Outline creator，通过 `quickPickDataSource`、`reveal`、`preview`、`captureViewState` 展示真实轮次／消息标题。活动 Thread 使用 `@` 查看其大纲，文本编辑器仍显示代码符号；`@:` 沿原 Provider 行为，不为会话新造代码符号种类。该设计利用原版已有的非文本分支，不重定义 `:` 为轮次。

大纲描述当前 Thread 的可导航记录，不代替工作区正文搜索。未载入历史时披露覆盖并通过历史 owner 补取或显式继续加载，不能把前端已加载页当作完整大纲。preview 及取消恢复仅改变该编辑器组的展示态；明确接受后能加载尚未挂载的历史记录并定位。没有真实记录时显示对应空态，不伪造符号，也不要求新建每轮 URI。

### 已确认的内容搜索覆盖

默认 Ctrl+P 查找资源名称、路径等信息，不自动搜索正文。只有用户主动进入内容搜索模式，才在当前明确范围内搜索选定类别；文件、会话消息、工具结果、LLM 请求记录、Trace／运行日志全部在功能范围内。类别选择和工作区范围是两个独立维度：切换类别不扩大工作区范围，没有结果不自动增加类别或改用全局搜索。

| 正文类别 | 接入依据 | 命中与打开语义 |
|---|---|---|
| 文件 | 原文件导航／搜索 owner | 文件引用、行列和有界片段，打开原文本或受支持视图 |
| 会话消息 | 原 canonical history／会话历史 owner | 精确 Session／Thread 与消息／轮次定位，不能把前端缓存或仅已加载消息当成完整搜索源 |
| 工具结果 | 原工具结果及历史 owner | 关联执行与具体结果的领域引用、片段和原视图，不因可读就开放其它私有文件 |
| LLM 请求记录 | 原请求记录 owner | 具体请求的既有归属、记录引用和详情，不当作聊天消息重复命中 |
| Trace／运行日志 | 各原运行记录与日志 owner | 明确日志类别与归属，定位具体记录或位置，不把不同 owner 的日志按展示路径混成同一文件 |

各类别通过自己的查询 owner 发布有界结果，统一浮层只组织结果和动作；不能用通用文件递归扫描私有存储代替领域检索。索引、规范 URI 与引用处理使用下述已确认规则，不再作为待确认项。消息列表、按 Session 读取整批 LLM 日志和现有 Trace 分页接口并不等于正文搜索：各 owner 必须补有界查询及按命中定位读取，禁止前端遍历所有会话、拉全日志后过滤。

消息正文的底层切片提供精确 `SessionThread/history/search`：区分大小写的字面量、非重叠匹配，每页默认 50、最多 100 个命中，最多检查 256 条正文、读取 8 MiB 已提交 JSONL，正文与 SQLite 查询调度共用 1 秒期限；原 snapshot／锁和本机同步 I/O 的准入规则继续生效，不声称能强行中断文件系统。SQLite 先在同一 active view 的完整替代范围择一，再有界取得正文 locator，不能因页外 final carrier 未返回就命中 stream shadow 或旧 canonical 版本。已有 internal history 的 stream 替代 CTE 和 survivor 同源复用，不创建搜索正文表；单项超过正文硬上限明确受限且不返回无法推进的 cursor。独立位置是 canonical item 可见文本中的零基 Unicode 码点半开区间，正文版本沿用原不透明 `content_hash`，不重新设计摘要语法。`around` 的可选 `anchor_message` 在同一快照核验位置后沿原分页链加载。核验响应同时返回一条完整 canonical 原文，不增加一次正文读取；由于常规历史详情有正文截断且 Markdown DOM 不保留原始码点位置，React 在对应 Turn 内临时展示并高亮该原文，等待 Virtuoso 实际挂载后滚动到 mark。关闭即释放，不进入时间线缓存、持久展示态或第二历史存储。

工作区 `POST /sessions/history/search` 在同一历史 owner 编排 catalog 与 SessionThread 的只读发现，每页至多检查 64 个目录位置、16 个 Thread；所有 Thread 共用上述 256 条、8 MiB 和 1 秒调度预算，准入等待共用最多 15 秒的总期限，并消费客户端传来的剩余期限。续页保存 catalog revision、当前 Session 控制库 Thread revision、keyset 与原历史 cursor；当前目录或历史变化明确冲突。各 Thread 是实际读取时的快照，已完成分支新增正文需重查。结果保留原 Thread 名称／归属和消息引用，空历史不物化，不由前端遍历 Session。消息定位通过 ThreadPane 的临时编辑器参数交给 React，复用原 around loader 与 Virtuoso 挂载回调；原文不进入时间线缓存。其它正文 owner 仍须各自实现后方可完成整项验收。

工具结果使用上述两个历史搜索入口的显式 `category=tool_result`，省略仍为 `message`，引用和两级 cursor 都绑定类别。同一 Turn／ModelCall／规范 provider call ID 的 stream 与 checkpoint 载体择一，与原 activity 归并一样取 `item_sequence` 最早的可见载体；两者落盘先后不固定，不能按载体类型猜测详情身份。跨 ModelCall 的同裸调用 ID 不合并。位置与响应继续用原 canonical item 身份，不建立工具搜索表。内联正文沿原投影读取；已截断工具结果只沿 canonical `artifact.tool_output` 读取同一 ToolOutputStore 的完整版本，JSONL 与完整输出字节共同消费原 8 MiB 预算，完整输出不预读 stat、不重复读取。读取时核对既有字节量和 `content_sha256`，以免旧位置被应用到被改写的外部正文；这是原不可变版本核对，不增加每键资源请求。

工具定位复用 `anchor_message`／`located_message`，around 在同一 snapshot 定点补取相应 ModelCall 限定调用的原工具详情。React 按 canonical item_id 找到原 ToolRow，展开其活动区与结果行后临时展示完整原文；mark 实际挂载通知原定位调度，原生 `scrollIntoView` 先揭示限高工具详情内的命中，再对齐外层虚拟列表。不得按同名工具或裸 call ID 猜测行。完整输出删除或改写明确转为 `stale_turn_reference`；权限与其它存储错误保留原错误归属。查询及打开均不触发模型或工具重放，关闭定位释放原文。

HTTP JSON 沿原 `requestJson` 统一在完整下载后解析，保留原非法响应诊断和下载取消；物理删除按体积切换到新 Worker 的分支。参考 Copilot 的普通 JSON 读取，并在真实 Chromium 对代表载荷测量：346,991 B 的同步解析为 0.4～3.6 ms、新 Worker 为 10.9～19.6 ms；8,882,087 B 分别为 12.2～26 ms 和 86.3～112.5 ms。该测量不代表原 around 响应或全链路收益。同步解析执行期间不可被取消，解析开始前仍优先检查取消；不保留第二解析实现。

### 索引归搜索 owner 决定

是否使用索引、索引技术、建立及维护时机、失效与重建均由各搜索 owner 根据数据性质决定。文件搜索可以按需查询，会话目录可以复用 catalog，消息或日志可以在其 owner 内采用必要的可重建索引；不要求统一技术或统一建索引流程。

Quick Access 不拥有索引，也不要求跨 owner 原始数据汇入全局搜索库。Gateway 联邦协调复用原 catalog 镜像及工作区 Provider，不建立消息、请求和日志的第二事实源；镜像来源和失效沿上文两级合同。使用索引的 owner 仍需说明查询覆盖、新鲜度、预算和结果受限情况；是否使用索引不能削弱原查询语义。

### 引用动作有两个独立目标

“附加到聊天”同时涉及来源资源和接收 Thread。接收者优先使用发起操作的 Thread，不因为搜索另一个工作区的资源而改为该工作区的 Thread；全局入口已有明确发起者时继续使用它，没有明确接收者时再用原版选择器让用户选择。不从最近使用列表、其它可见编辑器或后来获得焦点的标签猜接收者。来源与接收者确定后在本次流程中固定；接收者关闭、权限变化或能力不足时明确报告，不能改附加到另一个 Thread。

文件引用、粘贴内容与显式可复现引用分别处理：

| 动作 | 草稿中的含义 | 内容与版本处理 |
|---|---|---|
| 引用文件／资源 | 交给对应正文或上下文 owner 的资源引用，可含明确范围 | 不自动转成粘贴正文，不由 Quick Access 新增选择时或发送时的统一冻结策略 |
| 粘贴内容 | 用户选择的实际内容进入草稿 | 保留粘贴的内容，不在发送时悄悄用来源新内容替换，也不声称它仍跟随来源 |
| 明确需要可复现的引用 | 用户明确选择固定版本／快照或原操作已要求固定版本 | 通过原 owner 固定内容与 revision／hash 等结构化引用；不能取得所需版本时明确失败，不退回当前版本冒充复现 |

这些规则不放松已提交模型请求、canonical history、fork、变更快照或附件的既有冻结／恢复合同。Quick Access 只表达用户选的动作，不另建通用快照系统。已有上传附件、文件引用、来源激活不是同一合同；选择 URI 后应交对应 owner，不能直接包装成虚假附件或触发来源激活。取消未接受的选择不改草稿；加入草稿不自动发送或启动 turn。

### 搜索需要语义对齐，不能只换 UI

原版模糊匹配不能补回被后端子串查询提前排除的候选。会话和文件各自需要核对候选召回与排序语义；“前端拉全量再排序”和“每键搜索所有工作区”都不能当作通用解决方案。各 owner 负责满足已确认的查询行为及必要的召回改造，自行选择合适索引；共享层不强加全局索引或第二套查询实现。

沿用取消、debounce、快慢候选、busy 与结果分组。查询、范围、权限身份或 Provider 改变时废弃旧结果；预算、分页 cursor 与截断遵守领域合同。用户明确扩大范围后使用现有 Gateway 聚合时，要保留工作区新鲜度／失败状态，不能把不可达显示成空列表或全量成功。默认工作区查询不能调用全 Gateway 搜索再过滤回工作区，也不能因为无匹配、失败或较慢而隐式改用全局查询。暂停渲染旧请求结果与目标端执行停止分别验证。

### 查询请求、分页与定位的最小合同

这些是必须贯穿真实入口的数据语义。共享 Provider 及联邦信封只统一生命周期和归属，领域 payload 继续消费原 typed 合同，不建立通用业务对象平台：

| 边界 | 必须保留的信息与行为 |
|---|---|
| 发起上下文 | 打开前的 Gateway 归属、工作区稳定身份及已登记 route、焦点编辑器组、显式目标、可确定的发起 Thread；只在本次交互生存，不能写成新的全局“当前 Thread” |
| 一次查询 | 模式、原查询及匹配选项、明确工作区集合、正文类别、权限／用户会话代际、请求代际、各 owner 的预算；范围或类别变动产生新代际 |
| 一条命中 | 原 owner 身份、真实名称／归属／命中字段、高亮位置、有界片段、可用动作，必要规范 URI、版本或独立位置；不能用显示名作 action 参数 |
| 每个查询分支 | 工作区与 owner、pending／完成／受限／失败／取消、原 request_id、覆盖／新鲜度、cursor 及绑定的查询／数据 revision；只有全部所选分支成功完成且均无命中，才允许判定全量无匹配；失败／取消／受限单独披露 |
| 动作执行 | 冻结目标与接收者、用户明确的动作和参数、必要 expected revision、原权威回执；Picker 关闭不等于动作成功，也不撤回已被接受的业务写入 |

单工作区领域读取仍可经 Gateway 显式路由到原 owner；跨工作区查询统一由 Gateway 联邦 SearchService 调度，前端／工具／脚本不各自实现远端 fan-out。用户明确选择集合或 all，Gateway 先返回解析后的稳定目标集合，再对其中目标有界分发；现有 catalog 全范围接口必须按该合同演进并迁齐消费者，不能用于 A 或 A+B 后藏 C。不因范围选择而注册新 Gateway、建立连接、启动或激活停止的工作区；已有 catalog 镜像按来源及新鲜度披露，停止目标的启动／连接是独立动作。此处限制本次搜索行为，不废除既有控制面后台同步。

输入防抖和有限并发沿现有工具；用户继续输入时尽快取消排队／HTTP请求，并令全部迟到响应失效。正在执行且不合作的后端 owner 仍受总 deadline／扫描／字节预算约束，不声称 AbortSignal 能终止所有工作。各分支独立完成、独立显示错误，正常命中无需等最慢分支；busy 在全部当前分支结束或超时后解除。重试只重试明确失败分支，不重复已完成结果，更不重放写动作。

每个 owner 独立 cursor；“更多结果”由明确续查动作触发，不能每键拉全量。合并不共享一个虚假的全局 cursor／总数；达到任一上限显示该分支受限。cursor 失效时废弃对应页并明确重查，不拼接不同查询／revision 的页。只在范围、查询和身份代际都相同的情况下接受续页；合入后按稳定命中身份保持活动项及滚动。旧目标消失时清除动作确认，不能把回车应用到其原行的新对象。

正文首次进入显示类别选择，确认后查询，不预先搜索未确认类别；再次打开可恢复同一身份下的类别展示偏好但必须显示。正文至少支持字面量查询，文件继承原版选项；其它类别仅在owner正式支持时提供正则／整词等选项，多类别请求中不支持的分支明确错误，不能降级为另一种匹配而伪报完成。`%` 固定为文件类别，两入口复用同一文件搜索条件和 owner。会话消息保留 Thread／消息或 turn 的权威定位；工具结果保留执行关联及原结果身份；请求日志保留原请求记录身份；Trace／运行日志保留记录类别、归属和可校验位置。实现 owner 必须声明字符坐标单位及多行区间，适配原编辑器 UTF-16 行列，不能把字节／Python 码点直接当 Monaco 列号。片段不是完整原记录，不因截断而失去精确命中位置。

接受旧命中后，资源 owner 核验目标和必要版本，通过原 history／日志接口加载缺页，再打开对应 Thread、请求详情或日志视图。ThreadPane／React 历史定位按精确 owner、目标组和请求代际传递，虚拟列表未挂载不能只做一次 DOM 查找。记录变化或已删时明确“命中已过期”，不可找到原位置时不能退回顶部冒充定位成功；原可复现操作保持其冻结要求。

### 命令、上下文和引用的接入合同

项目动作注册一次 Action／Command，以同一入口供菜单、快捷键、picker 和现有 React 业务界面调用。优先级为显式目标／参数 → 发起编辑器的可确定 owner → 当前工作区级明确上下文 → 原版目标选择；禁止将 Session main 静默作为精确 child 的替代。状态与能力通过原 context keys／preconditions、菜单 `when` 表达，执行时仍向后端核验；缓存可用性只改善展示。

原版 `attach` 可能抢占修饰键接受；混合打开列表不接入原 ChatWidget 的 `lastFocusedWidget` 或另绑 Ctrl/Cmd 为“附加”。打开继续遵循原侧边／固定／后台组合；引用只从显式引用动作或只选择调用方发起，接收 Thread 使用本项目 Composer 合同。写动作接受后单独报告回执；取消未接受步骤无写副作用，后端已受理时 Esc 不伪称撤销。

### 继承原版机制的范围

| 机制族 | 沿用部分 | 项目需要明确的部分 |
|---|---|---|
| 路由与输入 | 空前缀、最长前缀、`?`、按上下文启用、保留输入、只选择模式 | 按已确认范围组织会话／Thread／文件；按已定组序、去重规则和项目专属前缀接入 |
| 搜索结果 | 模糊评分与高亮、描述／详情、分组、快慢结果、取消 | 领域召回、跨 owner 标注、分页和截断 |
| 编辑器导航 | `edt active `／`edt `／`edt mru `、历史、组、预览与固定、后台／侧边打开 | Thread 标签、去重、恢复与明确归属 |
| 文本定位 | `:`／`::`／`@`／`@:`／`#`／`%` | 后端文本与符号能力，不重定义原版前缀 |
| 命令与视图 | `>`、`view `、快捷键提示、上下文条件、命令历史 | 项目动作的目标解析和领域执行 |
| URI 打开 | 层级 URI 识别、Opener、编辑器解析、多种打开方式 | 规范 `boxteam://` 到领域资源的统一分派 |
| Quick Input | 单／多选、多步返回、输入校验、焦点、主题、ARIA／IME | 引用选择和必要的动作参数选择，不复制控件 |
| 其他原版贡献 | `term `、`task `、`debug `、`debug consoles `、`ext `／`ext install `、`mcpr `、条件性的 `issue ` | 不假定原生服务就是项目同名服务；保留原版语义与产品可用性条件 |

保留 `workbench.quickOpen.*`、预览及侧边打开设置、`search.quickOpen.*`、命令面板历史等原版配置。浏览器可能截获部分快捷键；Web 与 Electron 共用源码不代表宿主验收可以互相替代。“复用95%以上”不作为无法定义分母的验收数字，而按六类基准及全功能矩阵逐项证明。

### 全功能接入矩阵（不表示实施顺序）

“复用”表示保留原版机制并验证项目对象能进入；“接入”表示需要项目贡献或原 owner 补齐；“条件接入”表示先验证实际服务和宿主条件，条件不具备就明确不可用，不能做假列表或返回虚假成功。以下均为目标设计，不是当前已完成状态。项目新前缀按上文固定，不抢占原有前缀。矩阵同时列出注册前缀、前缀内子模式、URI 粘贴和键盘操作，不能把它们全部计为独立 Provider。

| 入口／功能 | 接入方式 | 项目结果与关键边界 |
|---|---|---|
| 无前缀 Ctrl+P | 接入默认候选，复用原匹配／历史 | 最近打开、会话、main／child Thread、文件，按明确工作区和类型分组 |
| `?`／Command Center 模式帮助 | 复用 Provider help entries 及 includeHelp | 项目模式与原版已启用模式一起可发现，展示描述和快捷键；模式接受保留上下文，默认模式不递归帮助 |
| `>` 命令 | 复用 Action／Command、菜单和上下文 | 既有会话／Thread、文件、任务、资源操作及面板聚焦统一进入；名称／别名／命令 ID 匹配和命令历史沿用原版 |
| `:` 行列 | 复用原文本定位 | 后端文本编辑器使用真实 selection，不解释成轮次编号 |
| `::` 字符偏移 | 复用原文本定位 | 只用于适用文本模型，遵守其偏移规则 |
| `@` 当前符号／大纲 | 接入原文本符号与非文本 Outline | 文本依赖真实符号服务；Thread 走 Outline creator，不伪造代码符号 |
| `@:` 符号分组 | 条件复用原符号分类 | 按原符号类别显示，不新造业务分类覆盖前缀 |
| `#` 工作区符号 | 条件接入原工作区符号服务 | 后端资源目标、业务搜索范围与符号索引需要对齐；没有真实语言服务就明确未启用 |
| `%` 文件内容 | 接入原文本搜索服务 | 文件匹配、行列和片段可继续转搜索视图；不静默扩张成所有业务正文 |
| 粘贴层级 URI | 接入统一业务 opener | 规范 `boxteam://` 交原解析与领域能力分派，保留多种打开方式和错误反馈 |
| `edt active ` | 复用当前组编辑器列表 | Thread、文件、Diff 共同列出，名称、图标、脏状态与关闭行为正确 |
| `edt ` | 复用按组编辑器列表 | 多组同一 Thread 的展示态独立，不能因关闭编辑器删除后端对象 |
| `edt mru ` | 复用 MRU 列表 | 最近使用准确，编辑器内部 URI 不能成为第二业务身份源 |
| 快速导航／修饰键释放 | 复用原 quickNavigate | 支持时保留快速切换；浏览器拦截的键位如实说明并保留可用命令入口 |
| `view ` | 复用视图查询和打开 | 会话导航与工作区终端／输出／端口／自动化／MCP 等原生贡献可发现，打开后显示正确归属 |
| `term ` | 条件复用原终端服务 | 仅表示已进入原生终端服务的实例；项目工作区终端通过真实资源 owner 聚焦，不用原生列表冒充 |
| `task ` | 条件复用原 TaskService | 原生构建／运行任务保留原语义，项目持续任务走其专属入口 |
| `debug ` | 条件复用原调试服务 | 原调试配置保留；Agent 调试、断点／快照和继续动作由项目命令交原调试 owner |
| `debug consoles ` | 条件复用原控制台服务 | 只列实际存在的调试控制台，不把任意日志当原生控制台 |
| `ext ` | 条件复用原扩展管理贡献 | 保留原安装状态和管理语义，不为快速访问新增原生扩展体系 |
| `ext install ` | 条件复用原扩展安装服务 | 仅在发行配置／网络／管理服务具备时提供真实搜索安装，不引入微软发行版外部服务承诺 |
| `mcpr ` | 条件复用原 MCP 资源服务 | 只有实际接到该服务的资源进入；项目后端 MCP 连接管理仍走其 owner 和命令 |
| `issue ` | 条件复用原问题报告贡献 | 按实际宿主和产品配置启用，不把浏览器宿主假定为完整桌面宿主 |
| `session `（项目） | 会话 catalog 候选 Provider | 按名称／ID／目录信息查找，folder 明确作为导航对象，打开 Session 时核验真实 main Thread |
| `thread `（项目） | Thread 领域候选 Provider | main 与 durable child 都有精确身份；对未打开 child 的召回缺口在原 owner 补齐，不只搜索已打开标签 |
| `resource `（项目） | 按类型和能力组织资源候选 | Skill、AGENTS、配置、附件、变更及运行资源等保持各自归属和可用动作，不靠通用文件扫描获得 |
| `automation `（项目） | 持续任务查询及原管理命令 | Goal 与其它任务共用权威任务对象，详情、运行记录、暂停／继续／检查等按状态可用 |
| `workspace `（项目） | 控制面工作区候选 | 选择范围与切换／启动工作区是不同动作；浏览候选不启动后端或隐式激活 |
| `content `（项目） | 明确正文类别的查询聚合 | 文件、消息、工具结果、请求记录、Trace／日志通过各 owner 查询，类别选择不扩大工作区 |
| 命令可用性与快捷键 | 复用上下文、precondition、快捷键配置 | 目标和状态决定可执行动作；不靠按钮消失代替后端校验，失效明确报告 |
| 行内动作／更多动作 | 复用原 item buttons、Tooltip、Quick Pick | 少量常用动作放行内，更多动作／参数使用原控件；打开不是引用／管理前置条件 |
| 分组与排序 | 复用模糊评分、MRU、分隔项 | 分组内部采用适用原版评分与历史配置；同一业务目标按 owner 身份核对，展示去重不改变业务身份 |
| 背景接受／连续打开 | 复用支持该能力的 Provider | 保持 picker 焦点、目标组和固定规则；不将连续打开语义自动应用于业务写操作 |
| 预览、固定与分屏 | 复用 EditorService、EditorResolver、组和 Grid | 所有入口交同一编辑器体系，不复制 Tab 或自建分屏；保持原文件树默认预览 |
| 资源只选择模式 | 复用 Quick Input pick 语义 | 可返回明确引用给原调用方，不要求先打开编辑器或直接执行业务 |
| 多选／复选框 | 在需要多个资源的选择流程复用 | 每项分别校验能力，取消不改草稿；不据此新增批量删除或向多个 Thread 群发的业务需求 |
| 多步输入／返回／校验 | 复用 Quick Pick、Input Box | 用于范围、接收 Thread 和必要参数；保持返回按钮与 Esc 各自语义，不另做向导状态机 |
| 树形选择 | 按适用流程复用 Quick Tree | 仅在确需层级选择时使用，不把默认 Ctrl+P 改造成另一套会话资源树 |
| 输入、滚动、焦点与加载 | 复用六类交互基准 | 取消旧请求、快慢候选合并、保留活动目标、IME／ARIA 等均按真实入口验收 |
| 历史与输入记忆 | 复用原存储及设置 | 资源／命令历史独立，移除与关闭／删除分离，过期引用不改投同名目标 |

每项验收记录需要指出：实际入口、数据 owner、目标归属、使用的原版服务、可用操作、未启用条件及可核查证据。条件接入不能成为随意不实现核心项目功能的借口：默认三类资源、五类正文与已有业务管理入口都属于明确接入范围。命令、原生终端、任务、调试、MCP 或扩展若缺必要运行服务，应记录实际缺口；不以“有该前缀”代替功能接入。

### 工程职责与依赖边界

| 位置 | 负责内容 | 不承担的内容 |
|---|---|---|
| `src/clients/workbench/src` | 原版 Registry／服务贡献、候选与动作组织、业务 opener、编辑器／视图分派及发起上下文 | 不复制后端状态机，不改固定 upstream，不导入旧 App 外壳 |
| `src/clients/web/src/api` 与可复用业务逻辑 | 现有协议请求、原领域动作、请求取消和权威状态对账；对现有调用方必要地抽出共享执行逻辑 | 不为 picker 复制一套协议客户端、隐式全局当前会话或假成功状态 |
| 工作区各领域 owner | 资源发现、正文查询、必要索引、真实权限／归属、生命周期与版本行为 | 不接受由 UI 标题或当前活动态猜测目标，不维护统一入口专属第二事实源 |
| Gateway | 控制面发现、显式目标路由、唯一联邦调度、增量汇总、稳定查询顺序及显式可比相关性模式，供 UI／工具／脚本复用 | 不持有 Agent 业务状态，不直接扫工作区私有业务目录，不强建全局正文索引，不重写原版 UI fuzzy／MRU |
| 工作区 SearchService／Provider | 轻量能力注册、typed 查询生命周期与原 owner 分派 | 不复制原查询器、不强制同一种索引、不把消息／日志伪装成文件 |
| 统一寻址 owner | 正式 URI grammar、按需模板登记和解析规则 | 不把所有命令／位置变成资源，不让前端另建 parser 或临时 scheme |
| Electron 宿主 | 沿用窗口、生命周期、固定存储位置 | 不因快速访问引入文件系统 IPC、扩展宿主或其它原生系统能力 |

这是一张职责图，不要求新建同名目录、抽象基类或通用适配平台。现有调用方与测试需要沿同一业务链一起迁移，原接口确已无直接／间接消费者后再移除，避免快速访问和原界面维护两套同义执行逻辑。

### 领域合同引用与补齐范围

本 change 的 `workbench-quick-access` 规范拥有新增交互和查询消费合同；以下原 owner 继续拥有业务事实，不因多一个入口就再定义一套模型。任务清单中的合同核对是代码实施前的必要条件，不是重新向用户收集已经确认的产品选择。

| 领域 | 具名合同依据 | 本 change 的补齐内容与界限 |
|---|---|---|
| 统一寻址及显式工作区 | `add-unified-virtual-resource-addressing` 的“多工作区场景下寻址层必须显式承载 scope_id 身份”“identity 必须独立于 URI 且不跨 scope 混同”“导航形态与操作能力必须由资源 owner 公布”；`add-multi-workspace-backend-mounting` 的“Gateway 必须显式传目标工作区且后端不得猜” | 使用稳定身份和显式路由；补内部编辑器投影，不新造公开 grammar。确需新模板时在唯一寻址 owner 登记，代码与消费者一起迁齐 |
| 会话目录与 Thread | `add-context-injection-lifecycle` 的“Context lifecycle owner 必须精确为 SessionThread”；根 AGENTS.md 的 SQLite catalog 规则 | catalog 名称／目录查询与未打开 durable child 发现仍由原 owner 提供；catalog 是导航、locator 和 main_thread_id 权威，Session 本地 thread catalog 是精确 Thread 发现依据，不能递归扫描磁盘重建 |
| 文件 | `resource-file-operations` 的“文件工具必须共用唯一寻址与文件 owner”“导航与搜索结果必须有界且可继续访问”“普通文本读写必须遵守能力与修订边界” | 补真实目录投影、模糊召回及已展示的文本搜索选项；读取与保存继续走唯一文件链和 CAS，不复制读写 owner |
| 消息与工具结果 | `session-turn-history` 的“Rollout 是聊天历史的唯一权威来源”“历史支持方向和游标”“历史读取有服务端硬上限” | 在原历史／工具结果 owner 内补有界正文查询及按命中读取；child 精确归属，查询 cursor 与历史定位 cursor 各用原合同，不能互换 |
| 请求与日志 | 原 LLM 请求记录及 Trace／运行日志的工作区后端 owner；`add-unified-virtual-resource-addressing` 的“Gateway 原始诊断日志管理入口保留原文” | 各类增加明确查询及定位，不把读取列表当正文搜索；工作区运行日志与 Gateway 自有诊断日志区分，只有用户明确选择有权访问的来源才查询，不因“日志”一词搜索所有 Gateway 日志 |
| 引用、来源及附件 | `session-context-resource-addressing` 的“三层分离”与持久引用合同、原附件 owner；`add-context-injection-lifecycle` 的 source／activation 合同 | 将已有引用、粘贴及显式固定版本交到指定 Composer；不新增默认冻结策略，不在预览时激活来源，不用资源 URI 伪造上传附件 |
| 任务与运行资源 | `persistent-task-management` 的“管理操作必须遵守后端权威状态”；`workspace-persistent-resources` 的“stop、detach 与 delete 是不同的资源操作” | 接入已有真实操作、完整回执及 revision，不新增调度器；终端／浏览器／MCP 的聚焦、停止、解除关联及删除沿原 owner 区分 |

已发现 `openspec/specs/scalable-session-navigation/spec.md` 中“物理目录树是会话位置的唯一权威来源”“目录索引可重建”等旧条文与当前明确的 SQLite catalog 合同冲突。本 change 不引用这些条文作为发现、恢复或迁移依据，也不据此重做目录架构；其余仍适用的分页／展示要求按现行 owner 合同核对。该主规范的历史同步属于原 owner 的规范整理，不混入快速访问实现；不得等待这项整理才遵守用户已明确的 catalog 规则。

实际新增或改变公开请求／响应时，在原 owner 内更新 schema／OpenAPI／生成客户端和对应 delta；沿既有合同消费时直接引用。领域字段、cursor 编码和预算数值服从原 owner，共享层仅按上文定义生命周期／归属／配额信封，不把领域数据重写成通用对象协议。没有相应 owner 的真实查询、定位和类型证据时，对应任务不能以“前端已注册”勾选完成。

### 范围选择与查询状态的展示合同

范围是入口的明确次要信息，选择动作使用原 Quick Input 按 Gateway／工作区归属组织已登记目标；返回按钮保留原用途。进入范围选择不修改当前查询，取消恢复原输入、范围和活动目标；确认后才产生新的范围代际并发起请求。允许明确缩小或扩大已登记目标集合，不在该流程自动连接、启动或注册目标。只有已确认的范围可进入历史结果与领域查询；范围选择本身可以读取必要的控制面登记信息，不以此授权读取未选工作区正文。

名称分支沿原 JSON 十五秒期限，把排队前确定的 UTC 截止时间通过 `X-BoxTeam-Name-Search-Deadline` 传给原 catalog／Thread API；Gateway 将联邦准入、排队和聚合纳入同一剩余期限，向下只透传或收紧，不续期。服务端取剩余期限与十五秒的较小值，原 owner 在冷快照逐行读取及投影、匹配和结果组装期间检查；未完成快照不进入缓存。Thread 的一秒调度预算仍用于分支续查，总期限另覆盖准入等待；异步取消仍等待原受保护 worker 结算，不提前释放其 Session 句柄。总期限错误沿名称 API 既有 409／request_id 出口；已开始增量响应的联邦请求则返回对应分支 typed 错误及结束状态，不把流结束当成功。非有限时间戳由 typed 合同拒绝，不新增搜索状态库或改写原 cursor／数据 revision。

| 当前状态 | 用户看到的反馈与可用动作 |
|---|---|
| 查询中 | 保留已到达的有效候选并显示 busy；模式和范围清楚，可继续输入或取消 |
| 全部分支成功且无命中 | 显示当前范围内没有匹配；用户可明确更换查询、类别或范围，不自动查询更多地方 |
| 部分失败／全部失败 | 按工作区及类别说明错误，保留其它有效命中，提供对应失败分支的重试；不得显示为完整零匹配 |
| 达到预算／尚有续页 | 标明受限来源及已知覆盖，提供该 owner 的明确续查；不显示虚假的跨 owner 总数 |
| 目标停止／身份失效 | 显示不可查询或历史镜像新鲜度，明确连接／启动是另一个动作；旧目标的执行按钮不得继续生效 |
| 用户取消或切换查询代际 | 结束本轮 busy、废弃迟到响应，不把主动取消当服务故障；已经接受的写动作仍按回执对账 |

错误反馈复用原选择器、通知及详情能力，不复制弹窗；搜索错误与业务动作错误分别归属。query 构建失败不能被原搜索代码的宽泛 catch 隐藏，用户至少能辨认失败目标、原因和可追查的 request_id。读取来源与展示错误不得触发额外业务写入。

### 历史与缓存保持展示用途

最近打开的资源／编辑器历史、最近命令、输入记忆与会话消息历史缓存是不同数据。复用现有 Workbench 存储与浏览器展示态存储，按 Gateway／工作区／用户身份边界隔离；不再为快速访问建立业务数据库。缓存允许先显示但要区分历史与当前事实，实际操作重新核验目标；从历史移除不删除会话，关闭 Tab 也不删除任务。输入记忆按模式保留，范围／凭据／接收者不随输入恢复；历史记录不得持久化 token 或临时 endpoint。Web 使用已有浏览器存储，Electron 使用既有固定 profile 存储，不增加 IPC；同一浏览器改连另一个 Gateway 或身份失效时，旧结果和动作立即失效。旧编辑器恢复数据通过一次明确格式升级读取稳定身份并重解路由，只保存新格式；旧数据不能恢复时明确提示并允许移除，不保留双写或长期两套解析。

## 架构大纲与跨模块影响（不表示实施顺序）

| 完整业务闭包 | 涉及模块 | 必须一起对齐的合同 |
|---|---|---|
| 快速访问入口 | Workbench 启动登记、Quick Access、原入口调用方 | 唯一默认／前缀描述符、捕获发起上下文、只选择／delegate／includeHelp 与 quickNavigate、六类交互 |
| 领域发现与查询 | catalog／Thread、文件导航、消息／工具／请求／Trace owner、工作区 Provider、Gateway 联邦服务 | 轻量能力合同、显式集合／all、范围发现、增量调度与稳定查询顺序；UI 沿原版交互评分，领域匹配／索引／精确定位仍由原 owner补 |
| 原文件搜索 | 文件内部根投影、FS stat、SearchService、QueryBuilder、`%`／SearchView／Search Editor | 根与本地模型身份隔离、全部已展示选项、查询转交、每个 Search Editor 的版本化 scope 与再次查询、已有保存／冲突规则 |
| 打开与恢复 | 统一寻址、Opener、Thread／文件／Diff输入及序列化、历史、分屏 | 规范URI和内部URI唯一投影、重解route、精确child、版本与selection、每组展示态 |
| 引用与管理 | Action／Command、菜单、React Composer／业务 hook、原后端写入owner | 单一动作入口、独立引用形态、发起Thread、pending／confirmed、revision及失败对账 |
| 非文本定位 | 原OutlineService、ThreadPane、React历史加载／虚拟列表 | 大纲覆盖、缺页加载、preview／reveal／恢复、目标组隔离 |
| 存储与运行面 | 原Workbench历史／设置、用户／Gateway屏障、Web与Electron宿主 | 展示态隔离、一次格式迁移、重启恢复、实际产物和原快捷键 |

依赖关系是：查询必须得到权威目标才可打开／引用／管理；URI投影、原编辑器恢复和结果定位必须一致；文件 root、SearchService、视图转交与 dirty model 过滤构成同一闭包。这些关系不能被拆成“前端先伪造候选，后端以后补”。索引技术仍各 owner自选，不因此增加统一对象注册平台。真正需要新规范URI时由寻址owner登记对应delta；仅查询字段和定位参数缺失由原领域合同补齐。对应业务代码实施前按这些完整链路确认所有调用方、协议生成物和测试；tasks.md 用于追踪工作与证据，不据章节顺序要求所有工作串行。

### 任务依赖与完成边界

下面只描述工作依赖，不指定实施排期。`tasks.md` 的组号供交叉引用；同一领域可在其协议、原 owner、客户端消费和定向验收闭合后独立交付，不能把全部任务视为串行流水线，也不能在其它 owner 未完成时把整个能力记为完成。

| 工作范围 | 进入实现必须具备的输入 | 完成时必须一起证明的消费者 |
|---|---|---|
| 两级搜索及联邦迁移（16） | 原领域查询与 typed 合同（1、3、4、9），原控制面登记、稳定路由及原版搜索行为 | Gateway 增量／期限／范围、工作区 Provider、旧新 UI 同链与删除清单、无浏览器工具／脚本、all 冻结及独立续页；已有前端切片勾选不能代替 |
| 各领域查询（3、4、9、11） | 对应的原 owner 合同及真实类型（1），不是全部领域都完成才允许推进 | 真实查询／动作入口、生成类型、对应 Provider／原界面消费、精确定位及定向测试 |
| 默认搜索（7、8） | 所接类别的真实查询（3、4）、明确身份与打开（2）；其它类别可独立实现但最终不得缺失 | Ctrl+P／Command Center／原文件选择器／原附加选择器，取消、排序、范围和接受行为 |
| 原文件搜索及视图（5、6） | 文件根和查询语义（4）、唯一 transform 登记（7.1） | `%`、SearchView、每个 Search Editor、原本地模型进度和保存／恢复，不只首屏结果 |
| 大纲与定位（9、10） | 原 history 读取和精确 Thread／目标组身份（2） | 未加载 child 历史、React 虚拟列表、Outline preview／reveal 和取消恢复 |
| 引用与管理（11、12） | 真实领域动作及其已有回执、来源身份与精确接收者 | picker、命令、原 React 界面与指定 Composer 同一执行链，错误后权威对账 |
| 展示态与运行面（13、14、15） | 上述相关输入／格式及其既有持久化能力 | Web 与真实 Electron 产物各自验证，覆盖全部明确功能；条件模式只按真实宿主服务判断 |

每一批源码变化先迁齐当前完整业务链，再集中完成相关静态分析及必要短检查；不在每改一个字段后重跑全套。原矩阵用例能证明相同行为时复用，不为任务组单独复制同一产品场景。独立工作区和 fixture 才能并行，有副作用的共享装具保持独占。若新的源码证据否定已定接入 seam，先修正设计和受影响任务，不能用旁路或第二实现绕过。

## Migration Plan

本次不切换服务、不修改用户环境。实施时在现有入口上单链路替换候选／动作，维持快捷命令、视图ID与当前文件树行为；清点旧调用方并迁齐后物理移除冗余逻辑，不设置“旧快速访问／新快速访问”长期双入口。编辑器／历史存储发生格式变化时明确版本和一次迁移，保留既有 working-copy／草稿持久化能力和每组视图数据，不能为迁移丢弃原本可恢复的未提交内容，也不将本改动扩张为新的通用备份体系；不得靠清空存储完成验收。失败撤回使用源码／产物版本回退，已执行后端操作不由UI回退自动反向执行。对不认识新展示态的旧版本明确拒绝对应恢复，不猜字段或跨身份回退。

2026-11-03 的两级搜索扩展只修改规划，不重新标记已完成的前端切片。新增迁移由 tasks 第 16 组追踪；实现时同一纵向链内迁齐 Gateway／工作区／客户端／工具及测试，不以保留两个可选搜索引擎作为过渡交付。原始业务存储、导航顺序、既有缓存来源及 URI 权威均不搬迁，公共协议升级前须完成其真实类型与直接／间接消费者清点。

## Risks / Trade-offs

- Gateway 联邦协调变成公共入口，若等待全分支会放大延迟 → 请求级增量、队列纳入总期限、独立分支状态与取消，不以可用命中冒充全部成功。
- all 的登记集合动态变化或不同 owner 分数不可比较 → 首次冻结稳定范围，续页沿原版本，公共名称排序有版本，正文保持分组，不宣称跨库快照或全域 top K。
- 原前端联邦和 Gateway 镜像形成双轨 → 清点并迁齐全部消费者，保留派生缓存来源，移除重复调度和独立领域匹配，旧证据不能替代新增无浏览器验收。
- 后端子串检索与用户预期的模糊搜索不一致 → 核对召回合同，不能只对截断结果排序后宣称等价。
- 现有虚拟工作区根与后端业务根不同 → 显式界定搜索根；不能只注册文件 Provider 就宣称接入完成。
- 多工作区同名、路由变化和多个分屏 → 用稳定领域目标核验，临时 route 仅为传输；失败不改投当前工作区。
- 模糊名称匹配误触发业务执行 → 候选高亮／预览无业务写副作用，开始／继续／删除必须明确选择动作，沿用领域确认规则。
- 统一入口演变为通用对象平台 → 仅共享必要目标与动作边界，领域查询和状态机留在原 owner，不预建通用适配层。
- 跨工作区缓存过期、权限撤回或目录异步操作 → 披露局部状态并重新核验，沿用 outbox 的 pending／confirmed 与原状态对账；不伪造成功。

## 验收维度（不表示实施顺序）

| 维度 | 必须证明的行为 |
|---|---|
| 真实入口 | 从实际 Workbench 的 Ctrl+P、`>`、URI 粘贴进入，经 Gateway 和目标工作区返回真实资源，不能只测试辅助函数 |
| 目标归属 | 两工作区同名资源、主／child Thread、多编辑器组、选择后切换工作区及重连，均不改投 |
| 动作独立 | 未打开详情即可管理；选中／取消不执行；引用不要求打开文件，业务命令不要求打开资源 |
| 原版交互 | 按六类交互设计基准逐项核对自动焦点、模式提示／帮助、匹配高亮、分组／图标／行内按钮、键盘导航、Esc／返回、后台打开、失焦设置、取消预览恢复、busy／增量结果／选择保持、输入与历史记忆；文件树默认预览不变 |
| 搜索边界 | 缩写召回与排序、旧请求晚到、取消、分页／截断、只读、不可达和部分失败可区分 |
| 两级联邦 | 显式集合／all 冻结、相关发现与领域查询分开、Gateway 唯一调度、Provider 能力、增量／断流／总期限、独立续页、导航顺序及规范 URI 归属 |
| 正文类别 | 逐项验证文件、会话消息、工具结果、LLM 请求记录和 Trace／运行日志的真实命中及定位；仅选类别被查询，默认入口不扫描正文，不扩大工作区范围，不把仅加载的前端历史当全量结果 |
| 写与引用 | 保存冲突、任务 revision、目录命令回执、权限变化；附加到指定 Thread 且不产生伪附件 |
| 恢复与隔离 | 最近资源／命令与聊天缓存区分；改名、删除、重启和身份切换不误开同名目标 |
| 运行面 | Web 与 Electron 复用贡献，各自验证启动／存储／快捷键；浏览器 parity 不替代桌面产物验收 |

以上是实现所需的验收合同，不代表已完成测试。实施进度与实际证据记录在 tasks.md；测试使用既有进程外保护和正式隔离产物目录，按影响范围选择，不默认串行全跑或无证据重跑。

### 可判别的验收场景与证据

| 场景组 | 必须设置的对照与观察 |
|---|---|
| 默认与原入口参数 | A／B／C含同名Session、main／未打开child、文件；焦点A导航B；输入和空输入历史仅A。`filesOnly`／fileSearchDelegate／filter／additionPicks／handleAccept／includeHelp／quickNavigate／资源只选择分别核实，不以单个Ctrl+P成功代替 |
| 请求范围 | 记录真实请求目标：只搜A没有B/C请求；明确A+B仍没有C；无匹配、失败、超时不扩搜；选择停止工作区不隐式启动／建立连接 |
| all 与相关范围 | all 解析为A+B后新增C／删除B，续页不吸收C或替换B；空／未知集合拒绝。相关范围先查控制面再以明确ID查树；已选all时B名字无关但正文独有词仍命中 |
| 联邦与无浏览器 | 关闭浏览器后真实工具／脚本发现工作区并查A+B／all会话树，保留分工作区根和父链；快A慢B渐进返回、断流未完成、排队过期不分发、原request_id不变，Gateway重启后独立续B且A不重查 |
| 排名及缓存 | 搜索排名不回写导航持久顺序；不同owner裸分数不混排，同名／同片段不同稳定身份保留。镜像明确来源／更新时间／stale，不能回答离线正文；旧React与Workbench同范围命中语义一致且无第二fan-out |
| 模糊与分页 | 名称存在间隔字符缩写但不含连续子串，放在首批结果之外；实际owner仍能召回或明确受限。旧cursor、revision变化、连续快速输入与慢响应，确认无串页／旧结果覆盖 |
| 原文件搜索 | A/B同路径不同内容且各有未保存模型，IndexedDB另有同名文件；Ctrl+P、`%`、正文文件类及搜索视图／Search Editor再次输入均只命中所选目标；两个范围的Search Editor同时分屏，复制、关闭恢复、保存／另存／备份、再次查询及旧格式缺scope均核对；非ASCII／emoji、多行命中位置准确 |
| 正文定位 | 每类仅有该类独有词，且对应child历史、工具结果或日志未被前端加载；查到后定位原记录。记录删除／变更拒绝旧定位，片段截断仍准确；其它正文owner没有被调用 |
| 交互和配置 | 原版键盘／焦点／预览／侧边／后台／IME／ARIA；历史关闭、相关性／recency、includeSymbols、输入记忆、失焦设置；`@` 文本符号与Thread大纲、取消恢复、未加载轮次 |
| 业务动作 | 多Thread同时可见，从A发起引用后切B／换搜索范围，只有A草稿改变且无turn；取消不改草稿；任务revision冲突、目录pending、写请求未知结果不自动重放 |
| 恢复和隔离 | 保存历史／分屏／草稿／脏模型后重启，route变化但身份相同仍正确重开；route被复用成不同工作区必须拒绝；换Gateway／身份后旧候选失效，删除历史不删业务 |
| 真正产品链路 | Web通过实际Gateway与目标后端；初始化 `/api/gateway/health`、`/api/gateway/workspaces`、`/api/v1/workspace` 及查询响应体request_id与X-Request-ID一致；Electron用打包产物验证快捷键、打开、动作和重启恢复 |

按闭包安排定向协议／owner测试、前端真实类型检查和web构建、真实浏览器及桌面验证；网络记录证明请求范围，不能仅靠UI看不到B断言未查询B。定向套件按资源／fixture隔离并行，同一有副作用fixture独占，昂贵全套只按影响扩展，不默认串行全跑，失败先归因不盲重跑。证据保留固定源码指纹、命令、进程外保护、原始输出、退出码和每项实际运行面；未具备条件的原生模式列缺失服务、宿主和原因，不用条件能力掩盖核心项目能力未完成。

## 后续细化边界（不表示实施顺序）

高影响的入口组织、范围、目标、查询生命周期、打开／引用／管理、历史与验收大纲按本稿固定。可在实现前进一步确定的只有字段命名、文件内符号拆分、基于原 owner预算的数值与规模fixture等工程细节；这些不得推翻已定合同。已获授权进入产品实现阶段，tasks.md 仅在对应实现和验证完成后勾选。后续真正需要改变用户可观察行为或原领域合同的分歧须再讨论，不能以“实现细节”为名静默改动。
