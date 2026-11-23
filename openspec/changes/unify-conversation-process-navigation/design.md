## Context

动机见 proposal.md。源码对照发现：

- `TurnActivitySummary.tsx` 的历史分支使用本地 `open`，实时分支是另一种摘要；`buildRenderGroups` 已能识别连续 work，但所有普通文本当前都作为 response 输出。
- `responsePartsToTimelineItems` 的 `id` 是 `part_id`，真实 Item 在 `responseSources[].source.item_id` 中，不能直接把组件 ID 当业务 Item ID。
- 现有后端 final projection 已使用真实 final identity。前端再找最后一段 Markdown 或拼接最长 assistant message 会丢失这个区别。
- `transcriptItems.ts` 与 internal execution merge 已持有 canonical logical order；内部执行明确允许 `turn_id = None`，无需伪造 Turn。
- 阅读位置仍是 `turn_anchor + scroll_offset`；滚动采集只看 `[data-turn-id]`，持久保存与可见节点身份脱节。浏览器存储实际上已按 Thread 分区，而旧规格仍写主 Thread 专用，必须一起修订。
- 原 `RolloutHistoryReader.inspect_item` 已提供 Item 的 Turn／Execution 定位；history 已支持 execution 查询和最多四个 execution Item 的正文回补，但 around 仍强制 Turn。复用这些原 owner，不新开服务。

参考 `/data1/hyf/20260822_agent/Kunlun-Code-CYX/docs/conversation-process-display.md` 及 `turnProcessPresentation.ts`、`ProcessActivityGroup.tsx`、`MessageList.tsx`，仅借鉴三层 v1 的交互，不复制其双层选项、历史兼容和业务状态推断。视觉继续复用当前 Workbench／Copilot 字体、主题变量、图标与菜单。

## Goals / Non-Goals

**Goals:**

- 实时、缓存首屏、历史刷新与无 Turn execution 使用同一内容归类、三层结构及导航坐标。
- 折叠、终态结算、虚拟列表分页和布局变化不使用户丢失阅读对象。
- 每个展开操作只读取其需要的受限投影，保留精确 owner、revision、失败语义与原性能限制。

**Non-Goals:**

- 不改变 canonical 存储、Turn／execution admission、模型回退、消息排队、持续任务或工具执行策略。
- 不新增全局消息树、持久活动组、搜索索引、后台连接、布局框架或 Electron 原生持久化桥。
- 不提供双层模式或保留旧实时／历史展示分支；不全仓改造与本链路无关的错误和性能机制。

## Decisions

### 0. 术语、坐标与版本的唯一含义

| 名称 | 本变更中的唯一含义 |
|---|---|
| 业务归属 scope | 完整 Gateway／工作区／Session／Thread；由既有视图记录持有，不以 UI 父链替代 |
| 过程归属 process_ref | scope 内真实 Turn 或无 Turn execution 的引用；不是新的业务 owner |
| 展示节点 transcript node | 具有独立阅读意义的正文、标题、标记或卡片；可包含子节点，不等同业务 Item |
| 来源引用 source_ref | 原业务稳定坐标或过程／组／消费边界引用；原 response part 的 source 字段保持原协议名称 |
| 展示部位 slot | 同一来源上的 body、header、marker、tool_input、tool_output 等稳定部位；不使用 role，以免混同消息角色与 ARIA role |
| 节点坐标 node_key | scope 内由 source_ref 与 slot 组成的结构化坐标；不包含折叠阶段、当前父容器、文案或像素位置 |
| 阅读锚点 reading_anchor | node_key、节点内位置 content_position 与视口相对偏移 viewport_offset_px；跟随最新是独立 follow_latest 意图 |
| 折叠状态键 | 节点坐标加必要的运行阶段；仅第一层区分 live／settled，不作为阅读节点身份 |
| projection_version | 原浏览器阅读记录的格式版本；目标2，不等同历史 projection_epoch、source_view_revision 或正文缓存格式版本 |

后文“恢复”指恢复浏览位置，“显式定位”指用户接受搜索、引用或跳转；两者共用控制器，但不可用目标的结果不同。本文的“游标”仅指后端不透明分页 cursor，阅读位置统一称“锚点”。“轮廓”统一称活动轮廓 activity_outline，不是正文摘要的另一份权威来源。

### 1. 一套过程投影，三层仅是展示结构

普通 Turn 使用 `{kind: turn, turn_id}` 作为 process_ref；无 Turn execution 使用 `{kind: execution, execution_id}`。普通 Turn 内的 retry／fallback 保留真实 execution 来源，仍处于同一 Turn 过程。内部执行按 canonical 成员位置插入主时间线，不能把跨越其它可见内容的执行成员强行搬到一起；非连续的展示段用该段首个稳定成员区分，共用 process_ref 而不改变原顺序。

```text
用户输入／附件
过程摘要：状态 · 耗时 · Item · 当前活动                  第一层
├─ 思考 N · 工具 N · 当前活动                           第二层
│  ├─ 思考／安全摘要 → 可读正文                          第三层
│  └─ 工具标题 → 参数和结果                             第三层
├─ 普通中间 Markdown
├─ 独立压缩标记或上下文通知（保留原语义与坐标）
└─ 下一组连续思考／工具
真实最终回答
```

普通中间文本、压缩 Item、上下文通知会结束当前活动组，不能被并入 thinking。上下文通知保持原请求／消费边界；Turn 外的通知仍在 Turn 外。等待用户处理的独立入口、必要失败恢复动作和产物入口始终可达，折叠不改动它们的状态或草稿。没有过程成员时不制造空的三层壳；失败或活动统计仍可显示为静态行，不提供无内容展开按钮。

活动组的 source_ref 使用 process_ref、实际展示段及组首成员的 `(item_id, part_id)`；不能用数组下标、分组数量或当前活动文案。分页只拿到组中段时沿同一 reader 取得真实边界的轻量坐标，不用当前页第一项冒充组首。权威成员追加只延长组，不改变其身份。活动组是派生结果，不落业务库；活动组父链只表达可见性，不进入普通内容节点的 node_key。

### 2. 最终回答按真实 final identity，流式正文不提前冒充 final

沿现有 `final_item_id`／`final_text` 和 response source 判定；保留文本部件的原 kind，不能转成 Markdown 后丢失 final 区别，也不再取最长 assistant message 或最后一个 Markdown 兜底。

尚未有权威 final 的流式文本在第一层内部持续呈现；运行时第一层默认展开，因此用户仍看到流式回答。手动收起后流式文本留在其中。原权威终态及 final 身份可用后，同一次展示提交完成过程外回答与终态折叠，不以额外历史请求作为“先显示 final、再补摘要”的串行前置；终态校准仍沿原链路有界进行。已确认相同业务来源的正文只改变展示父节点，slot 保持 body；不能因为处于 final 区域而另造节点身份。失败、中断或未知执行的部分输出保留在原过程内，不伪装成功 final；后端明确的空成功不制造空回答。

实时临时 Part、持久 activity Part 与最终投影 Part 不保证字符串 ID 相同：当前 mapper 可分别产生 `item:part:n` 和 `item:final`。只沿原 producer／内容部位／canonical source 的确定关联重绑定锚点及折叠选择；必要时补齐原投影的来源关联，不改写真实 Part ID、不按文本相等配对。无 Turn 内部 execution 沿原独立 envelope 与 display_parts／response_parts 合同，不新增 Turn final 字段；只有原权威投影确实提供 final 身份的内容才进入独立回答区，否则保留原输出类型及独立安全 notice，不把最后一段内部文本猜成 final。

一个 final 确实聚合多个 Part 时，沿权威内容部位关系定位，不能随意沿用其中某一个 Part 的状态；没有可确认关联时终止临时 Part 的精确保位、按过程标题规则结算并说明精确位置未保留，不冒充同一节点。不会将未确认的临时映射持久化或建立全局别名表。

这意味着最终回复在得到 final 确认前属于正在执行的内容，不能既保证未知终态的文本永远在外，又保证它绝不是中间说明；选择沿权威 final 结算，配合锚点保位处理一次必要的结构变化。

### 3. 折叠状态只有一个展示态 owner

第一层 live（所属过程尚未真实终态，包含等待开始）默认打开，settled 默认关闭，第二、三层默认关闭。只有第一层折叠状态键带 live／settled 阶段，标题 node_key 不带阶段；只有所属 Turn 或内部 execution 的真实终态才切阶段，单个模型／工具 attempt 或关联 Job 完成、连接断开、组件重挂载、详情加载和普通文本到达均不能代替过程终态。运行时的打开选择不阻止终态默认收起，终态后的手动展开不会被历史重放再次收起。第二、三层的选择不因第一层收起被删除。

内部 execution 的终态证据复用原存储 `read_terminal_result().commit_id`，由原历史 envelope 的 `terminal_commit_id` 传递；缺省表示尚无终态提交，不从关联 Job、输出存在或页面是否历史模式推断完成，不另建终态账本。

折叠状态进入原浏览器视图记录，只保存相对默认值的稀疏覆盖；逐项状态替换现有重复的局部 open 与同义折叠状态。原 tool_details_expanded 若控制原始协议字段展示而非折叠，保留其独立语义，不误当第三层开关。虚拟化暂时卸载不能删除用户选择；清理仅作用于权威已移除成员，或沿原视图记录生命周期执行，不新增定时清理器。

### 4. 统一展示节点合同与阅读位置控制器

#### 4.1 归属与实现规模

```text
原业务 owner：历史／Item、上下文、附件及未来资源
    ↓ 稳定来源、权威顺序、存在性与原查询能力
现有 transcript 投影：生成节点身份与展示父子关系
    ↓
原虚拟列表＋统一阅读位置控制器：采集、定位、测量、保位与结果交付
    ↓
原浏览器视图 owner：单一 reading_anchor、折叠覆盖、follow_latest、格式版本
```

“统一 owner”只针对展示态与阅读行为：现有视图 owner 持有持久记录，当前编辑器的阅读位置控制器持有一次定位及临时保位状态。它们不能接管资源 CRUD、存在性、业务迁移、执行生命周期或后端数据事实。原 history／上下文／附件 owner 仍使用原入口；不存在所有业务资源统一迁到一个节点服务的步骤。

内置展示组件静态遵守同一合同即可，不新增运行时扩展注册表、服务发现或可持久节点目录。DOM 挂载／卸载只更新当前可测量元素引用，复用虚拟列表和已有尺寸观察设施，不是创建／删除业务资源；不把全 transcript 再复制成第二份状态树。第三方展示贡献接口不在本次范围。

#### 4.2 稳定来源与展示部位组成节点身份

一个业务对象可产生正文、标题、附件等多个节点。轻量合同只表达：完整 scope、稳定 source_ref、展示部位 slot、可见祖先关系和可测量元素；需要远端加载时沿 source_ref 分派到原业务 owner。持久记录只保存可序列化坐标，不保存 DOM、回调、整条祖先链或内容镜像。

| 内容 | 稳定来源与展示部位 | 持久锚点资格 |
|---|---|---|
| 消息、思考、工具内容 | 真实 Item／Part＋正文、参数或结果等部位 | 有 |
| 第一层、活动组标题 | 真实过程／组边界＋标题部位；非连续段保留段首坐标 | 有 |
| 可展开附加数据虚线 | 原上下文消费请求／分组边界＋标记部位 | 有 |
| 附件或未来数据卡片 | 原稳定资源来源＋卡片部位 | 有独立阅读意义时有 |
| 装饰线、间距、边框 | 从属语义节点 | 无，几何变化由保位处理 |
| 加载提示、骨架和虚拟占位 | 从属真实待加载节点 | 不另存身份，不冒充真实内容已定位 |

节点身份不得依赖当前行号、DOM 路径、数组下标、文案、尺寸、随机 ID 或所有子项 ID 的拼接。当前上下文虚线的 `data-context-marker` 拼接列表只作成员说明，不是整条虚线的身份；以真实消费请求 model_call_id 和 anchor 类型识别同一过程内的整条虚线。before-item／anchor ordinal 只决定插入位置，输出由占位转为已提交 Item 时不得改变节点身份。补读更多来源不改其 key。附加数据详情按各自真实来源识别。

新增独立占行内容只接入来源／展示部位、可见祖先、原加载归属和测量位置；已有可定位来源不应要求修改通用滚动算法。只有新业务来源缺查询能力时才由其原 owner 补有界定位，不能用泛化注册服务掩盖缺失 API。source_ref／slot 的唯一性在当前编辑器生成节点和实际解析时保证，不每次滚动全量审计或 hash 全历史。

#### 4.3 只保存一个锚点，布局变化持有原目标

SessionViewState.reading_anchor 保存 node_key、content_position 与 viewport_offset_px；其中 viewport_offset_px 是所定位内容点相对滚动视口顶部的有符号 CSS 像素差，不是节点高度或绝对 scrollTop。content_position 优先使用已经存在的稳定 Part／正文块坐标，其余只作受节点范围限制的像素微调。长正文不能重新退回整轮大像素偏移；宽度变化后保证相同内容附近，不承诺任意 Markdown 重排后精确到同一字，也不因此新建全局 Markdown 字符索引。

实际可见祖先由当前投影计算，不另存一份“真实锚点／可见锚点”双状态。当前采集选择与视口阅读基准线相交或紧邻的独立语义节点，忽略装饰及加载提示，避免同时把外层 Turn 包装器与其内部节点选成两个竞争锚点。没有当前目标时按可见节点建立锚点；用户主动滚动或明确操作改变阅读目标时才更新。布局更新期间继续持有原稳定目标，不能每次 DOM／ResizeObserver 更新都重选第一条可见项，否则上方新增虚线会抢走锚点。

尚未提交 canonical Item 的流式部件只以真实流实体／临时 part_id 在当前编辑器内保位，不能伪造 item_id、sequence 或新建全局 part 索引。提交后的来源关联按第2节规则核实并重绑定，不能要求临时与持久 part_id 相等；持久保存前若仍无可恢复业务来源，则保存真实过程标题与 follow_latest，不将临时流坐标宣称为可跨重启恢复的 Item。

follow_latest=true 时以尾部跟随为布局目标，不同时执行节点锁定补偿；显式定位暂停跟随，用户主动滚动立即中止旧定位／补偿。折叠与尺寸变化本身不改变跟随意图。内容不足一屏或目标接近首尾时，若原 viewport_offset_px 超出合法滚动范围，则一次钳制到可达到的位置，不新增空白垫高、不无限重试；这属于几何限制，不是资源缺失或精确位置恢复成功。

| 事件 | 确定行为 |
|---|---|
| 手动收起正在阅读的祖先 | 将当前锚点改为该标题；键盘焦点仅在原来位于被隐藏子树时回标题 |
| 收起视口上方内容 | 保住当前可见内容；不滚到被点击标题 |
| 终态自动收起 | 当前内容将被隐藏时转到过程标题；若 final 仍可见则继续锚定 final |
| 刷新或返回 | 读取折叠选择 → 按需加载锚点窗口 → 挂载可见节点 → 应用目标内偏移 |
| 恢复的 Item 已被折叠隐藏 | 定位最近可见祖先，不自动展开；内容坐标无需另存一份可见坐标 |
| 搜索／引用／明确跳转 | 暂停跟随最新，按需打开必要祖先，定位 Item／Part；不展开其它活动组或全部工具 |
| 流式追加、prepend、宽度／图片变化 | 按稳定坐标保持当前可见位置；仍跟随最新时维持尾部 |
| 已挂载节点被虚拟列表卸载 | 保留原坐标，沿虚拟列表挂载／定位，不推断业务删除 |
| 权威确认来源或展示部位已不可用 | 普通恢复可使用可见祖先或同 Thread 有界确定的相邻位置，明确原目标未恢复；显式跳转明确失败，不以相邻对象作为成功 |
| 查询超时、权限／归属拒绝、存储或后端故障 | 交付原失败，保留阅读记录和可用界面，不进入相邻位置／最新消息替代路径 |

当前视口采集必须验证元素确实与视口相交，不能选择视口下面尚未可见的 Turn。程序性恢复、补偿及自动收起不能误判为用户主动滚动，也不能反复互相触发。

规范 Item URI 交原 Opener 核验位置后统一进入该 Thread 定位，不以缺少 turn_id 为由改读原文；原文仍是独立菜单操作。内部 execution 的分段标题以原 `header:<段首 Item ID>` 区分，缺段时用该真实 Item 沿原 around 入口读取。同一 execution 的其它段、独立回报或空标题不能代替目标分段，不增加坐标字段或执行尾页兜底。

已有 execution 分段标题可直接恢复，即使其隐藏正文已被内存回收；原空执行的通用标题被真实分段替换后，明确表示旧位置不可用，不无限等待旧 DOM。上下文标记缺页时，原 history 请求附带 `anchor_context={model_call_id,anchor}`，只与该 Turn 的首次 around 或该 execution 的首次 tail 配合。原 reader 在同一 snapshot 内按指定过程、消费请求和标记类型定点读取至多128项元数据，不读来源正文、不遍历 context_cursor；该组超限时 context_cursor 冻结同一选择并显式续读，不改主时间线游标。确认无可见标记时沿原结构化404回执返回 history_locate、来源选择和 request_id，阅读恢复报告不可用且保留原记录；实际存储／版本错误仍传播原原因。

#### 4.4 同一入口区分可见性、缺页与真实失败

控制器统一交付定位结果，复用原导航任务及错误呈现，不新增全局异常服务或持久结果账本：

- 找到真实目标：完成定位；普通恢复遇到折叠内容时可完成到可见祖先，结果不能声称具体隐藏正文已经显示。
- 未加载／虚拟卸载：属于尚未完成，通过原加载与挂载流程推进；没有 DOM 本身不能证明记录删除。加载失败按失败结束，不自动升级成 unavailable。
- 已确认目标不可用：只有原 owner 确认当前视图无该资源，或当前支持投影明确取消该展示部位，才进入 unavailable；Part 不可用但 Item 可见时，普通恢复可到 Item 标题并说明，显式精确跳转仍报告 Part 不可用。
- 格式不支持／来源无法识别、记录损坏或请求失败：保留原记录并明确报告，不能按不存在处理，不能清库或滚到最新冒充成功。

相邻位置仅在普通恢复已确认 unavailable 时使用，范围局限同一 owner 的已知窗口或原查询可直接确定的邻居；无可确定邻居时显示正常初始窗口／空态并说明未恢复。不得逐页穷举、扫描全历史、无限重试，亦不得把所有异常 catch 后归到这一分支。原 owner 返回的错误码、request_id 和原因沿项目既有失败合同传播。

### 5. 所有 Thread 隔离，多个编辑器不相互拉动

持久 scope 沿既有浏览器入口使用入口 Gateway、资源 Gateway／工作区、Session、真实 Thread；不能以端口、路径、显示名称或已退役 user_id 代替稳定身份。main 解析到真实 Thread 后必须统一读写键，不能与字面 `main` 形成两份状态。

同一 Thread 在不同编辑器各有运行中的折叠／滚动快照；不通过 BroadcastChannel 强制移动其它已打开编辑器。持久默认位置由最后实际交互的编辑器保存，程序性挂载／恢复不能抢写；不新增窗口注册表或每编辑器永久存储。新打开该 Thread 恢复最后保存位置，fork 和新 Session 不继承来源阅读记录。Web 与 Electron 沿同一前端存储实现，宿主仍只负责固定存储位置。

### 6. 扩展原 history 查询合同，按层交付

在原请求及原 reader 内完成以下闭环，DTO／proto／生成代码／调用方同批迁齐：

1. 增加 `anchor_item_id` 的 around 选择，与 `anchor_turn_id`、显式 detail owner 互斥。原 snapshot 内沿现有 Item metadata 查询核验真实 owner、可见性与逻辑坐标；普通 Item 返回 Turn 附近窗口，内部 Item 返回逻辑坐标附近的有界混合历史窗口。响应增加 `located_item`，携带真实 `ThreadItemLocationDTO`，不重写来源 lineage。
2. 统一已有最多四项正文回补为 `item_ids`，替换 `execution_item_ids` 的专用字段；通过原 Turn 或 execution selector限定 owner，核验所有 Item 属于该 owner／当前可见历史。详情回补不推进主历史游标；工具按原 tool-call 关联加载，不能将大结果塞进轮廓。定点响应同时提供所选成员必要的真实组首／展示段边界元数据，供挂载必要标题及目标；不要求为跳到早期成员先从活动尾页逐页扫描，也不宣称中间未读成员已经加载。
3. 在原 include 投影加入 `activity_outline`：有界返回顺序、Item／Part 身份、kind、组边界、工具名／状态／已有安全摘要与时间，不返回 reasoning、参数、结果正文。普通中间文本按原 assistant_text 投影有界读取；超预算有明确 continuation／truncation，不能以截断假装活动已完整。
4. 第一层展开取得中间文本与轮廓；第二层消费已有轮廓，仅补读缺失轮廓页；第三层才回补所选 Item 的受限正文。相同已满足投影不重复请求；折叠请求期间返回的数据可以缓存，但不得重新打开用户已收起内容。

轮廓边界从原 canonical/index 查询派生，不增活动组表。不能在每次暖读物化整轮正文再丢弃、不对全历史 hash／扫描；必要局部边界查询若缺索引，索引归原存储 owner 和原迁移链，不建立第二目录。保留 `tool_call_ids` 等已有独立业务查询，但前端三层正文读取只走统一 Item 回补，不为实时与历史另造正文 API。

活动轮廓首次请求使用 `direction=tail`，显式选择单个 `turn_ids=[id]` 或单个 `execution_id`（互斥），include 含 `activity_outline`，可加有界 `assistant_text`，内部执行同时保留 `internal`。不得混入 around、tool_call_ids、item_ids、context_cursor 或主窗口数量选择器。服务在原 opaque cursor 中以 `query_mode=activity` 冻结这个 process_ref、include、目标 owner、view／revision、首次成员上界及页内成员坐标（需要时含原 part_ordinal，避免同一 Item 的部件越过预算后被跳过）；该模式由轮廓请求派生，不增加前端 mode 开关。每页按原字节上限和至多256个 canonical record 限额读取，按权威顺序返回，通过原 `before_cursor` 以 `direction=before` 向前续读；省略 selector／include 时消费冻结值，显式冲突拒绝。复用原 codec／snapshot／keyset 原语，不建第二 token 格式或 execution 专用 phase。已读尾部先显示，未读前部有明确补读入口，不能表示整个过程已完整。

活动轮廓 continuation 只属于该过程的详情读取，不替换主时间线 before_cursor／after_cursor，也不能借用只服务上下文 metadata 的 context_cursor。原 execution 默认查询仍用既有 execution 模式；仅显式 activity_outline 进入统一活动模式。`item_ids` 定点回补要求一个明确 process_ref、direction=tail、无 cursor，与轮廓续读及其它定位选择器互斥；其最多四项是原正文回补上限，不是轮廓页上限。单项正文达到原硬预算时保留原截断事实与已有完整输出／详情入口，不通过加大上限或自动拉全解决。

正文回补只丰富既有 canonical part，不重新推断身份／顺序／计数。完整 owner 投影按原规则整体替换该 owner；轮廓页及选中 Item 片段由同一个 canonical reducer 按真实 part／source identity 与权威顺序应用，仅替换其明确覆盖的范围，保留其它已加载成员。同一 binding 的轻量轮廓不得降级已取得正文；较新 binding 使不兼容详情失效，迟到旧响应不得回写。覆盖范围复用请求选择器、响应身份、revision 及原 continuation／truncation，不新增持久覆盖账本，不把响应缺省当删除，也不以手写字段补丁或文本去重拼成第二权威对象。

首次活动轮廓和 item_ids 定点回补在原 TurnHistoryLoadRequest 成对回传已取得页面／summary 的 source_view_id／source_view_revision，服务在同一 snapshot 核验；仅当前视图匹配时读取，不重定向到另一个 view。cursor 续读可省略这对值而消费 token 冻结值，显式提供则必须一致；缺失一半、归属错误或版本漂移沿原 invalid／stale 历史错误合同拒绝。选择器、binding 和 budget 在 reader 入口统一校验，不由每层组件重复检查。无已取得投影的普通主窗口初读不要求此详情 binding；实时未提交 Part 不得伪造这些值或提前走历史详情。响应复用原 page／summary 的 binding，不增第二版本账本。

分页 cursor 继续绑定原 view／revision／include 与查询范围；不把阅读锚点塞入实时传输游标。先接通已有 reader 的选择方式，再补当前缺口，不能保留旧 execution-only 字段别名。结果被旧请求返回时，按现有 owner／取消规则丢弃；缓存首屏与后端校准也使用同一投影与阶段键。

### 7. 摘要布局、语义与时间

各层一行、箭头靠右。宽度收缩时依次隐藏附加用量、当前活动、Item／组计数；保留开关、状态图标及可信计时／耗时，不用横向滚动或增加摘要行高。详情使用原 Workbench 结构化悬停，字段为状态、耗时、Item、当前活动、最近请求；不用一段拼接的长字符串。键盘可展开、收起并读到等价信息，切换不挤动整页布局。

顶层展开时不重复转圈；运行中收起才在摘要提供活动指示。活动使用真实事件，优先工具目录本地化 display label，缺失用通用“正在调用工具”；并行时取尚在运行且创建最新者，不从参数猜描述。单次工具失败不改变整轮成功或强制打开任何层；断连不当终态，未记录结果如实显示未知。

耗时沿现有权威统计与目标时间源，运行态仅用单调时钟推算，终态冻结；并行耗时不累加。仅协议允许缺省的运行态／内部执行统计缺可信来源时省略数字；历史 Turn 必填时间缺失、倒退或 revision 不一致仍按原合同返回 projection 错误，不由展示层省略掩盖。Item 使用既定 canonical 计数，不按组件、组数或分块重新统计；无 Turn execution 缺现成统计时只使用原 admission／成员元数据，不伪造 Turn。最近上下文用量绑定真实请求／模型，未知就省略，不从模型名推测容量或混合多个 Provider 的上限。

### 8. 目录落点与删除范围

在原职责目录内修改；确需新文件时只提取有独立职责的过程分组／坐标模型，不新建大而全的 presentation service。建议落点：

| 职责 | 原位置 |
|---|---|
| canonical part 到展示节点／活动组 | `state/timeline/responseParts.ts`、`chatResponseGroups.ts`、`components/chat/transcriptItems.ts` |
| 轻量节点合同与元素绑定 | `state/timeline/transcriptNodes.ts` 与 `components/chat/transcriptGeometry.ts`；各语义组件静态使用，不建立运行时注册服务 |
| 三层过程与内部执行接入 | `components/chat/turn/TurnActivitySummary.tsx`、`InternalExecutionTranscriptItem.tsx`、`components/panels/ChatPanel.tsx` |
| 锚点采集、解析与保位 | `components/chat/useTranscriptVirtualScroller.ts`、`useTranscriptNavigation.ts`；不保留旧名转发包装 |
| 展示态持久化 | `components/chat/TranscriptViewContext.tsx`、`state/sessionViewState.ts`、`hooks/session/browserSessionViews.ts`、`useSessionViewState.ts` |
| 历史窗口／详情 | `api/session/sessionTurnHistory.ts` 与原 rollout history reader、mapping、proto |

删除旧 `ConversationWorkSummary`／历史专用展开分支、最后 Markdown 判定 final、Turn 内长距离偏移恢复、重复折叠状态 owner 与多余导航类型约束。删除前全仓检索调用方、测试、缓存序列化和独立 child 编辑器消费者，保持一条纵向链路。

### 9. 性能与验证边界

继承 `interactive-latency-budgets`；本链路沿已有更严格历史预算：128 Turn 混合 fixture 下后端 history 暖读 p95≤200ms（优化目标100ms），浏览器 prepend／append 与位置恢复每次≤200ms。新增第一、二层暖轮廓请求同样使用该后端预算；已有数据的单次折叠／展开与保位在200ms内完成到真实可用 UI。需网络补读的展开从点击计至目标可见可操作，普通暖请求场景 p95≤500ms、单次≤1500ms；首次未缓存读取≤3000ms，不用提前显示 loading 冒充终点。

复用既有 fixture 和 deterministic stream stub，通过真实 Workbench → Gateway → Workspace 入口验证；不运行 reference 仓库测试。保留20次暖请求原始时长、p50/p95/max、request_id、字节数、实际结果、源码指纹、命令与退出码。断网／模型等待单列，不能从内部统计中扣除排队和校验。仅用少量有判别力的投影／坐标测试覆盖稳定机制，不为高频变化文案、CSS 类名或每个计时 tick 写测试。

暖 history 采样复用产品已有的凭据缓存语义：先准备一次本地凭据，再测完整 Gateway history 请求、正文接收、JSON解析及浏览器往返。凭据准备耗时单列；不得每次额外请求凭据并计作 history 自身耗时。冷页面初始化和首次读取仍覆盖其实际准备成本，不能把业务内部排队、校验、代理或序列化从计时中移除。

## Risks / Trade-offs

- [实时 final 尚未确认] → 文本先在已展开过程内流式呈现，真实 final 后一次结算，按真实来源关联重绑定坐标；不承诺临时 Part ID 不变，不靠文本位置猜 final。
- [长过程轮廓也可能很大] → 同一 reader 的字节／成员上限和 continuation；第三层正文限量补读，不让第一层展开物化全部活动。
- [内部执行交错或分段] → 保持全局 canonical 顺序，以真实段首区分展示位置；不能为整齐而重排。
- [异步尺寸变化引起恢复循环] → 使用同一定位事务和稳定节点，区分用户操作与程序补偿，结束后解除观察；不堆叠定时重试。
- [将 DOM 缺失误当删除或故障吞进兜底] → 可见性与加载由控制器处理，资源存在性由原 owner 确认；统一入口传播真实失败，只有确认不可用的普通恢复使用有界替代位置。
- [新增卡片导致滚动分支不断膨胀] → 组件只接入稳定来源、展示部位、祖先和测量合同，滚动算法不按业务类型枚举；不新增服务注册和节点持久化。
- [已打开多个同 Thread 编辑器互相抢写] → 各自保留运行态，只有实际交互更新持久默认位置，不做跨编辑器强制同步。
- [其它变更同时修改 DTO／failure] → 实施前核对共享文件写入方；复用 `unify-failure-reporting` 原字段，不另建错误 envelope。

## Migration Plan

1. 按版本升级约定核对实际 release 支持基线、原视图 writer 与来源格式；当前已核源码写入 `projection_version: 1`，目标为同一 owner 的 `projection_version: 2`，不提升无结构变化的 IndexedDB schema version，不新增账本。
2. 缺可核验 release 基线时继续当前目标实现，不为 dev 中间态添加双读／猜测转换。发现旧格式或损坏视图时保留原记录并明确说明未恢复，不清库、不影响 Composer 草稿／outbox／凭据，也不以跳到尾部声称恢复成功；用户后续明确浏览产生的新位置由新 writer正常保存。
3. 若核实受支持发布来源确需转换，在原 view owner 的迁移入口按真实来源与目标格式命名，并登记必要步骤；转换不能把 Turn 偏移伪称精确 Item 位置。部署前落实该受支持来源的保留与恢复验证，不能把此项留作实现后的隐含兼容需求。
4. 先迁齐协议与同一 reader，再迁齐投影、三层组件、视图 writer／reader、导航和所有直接／间接消费者，删除旧链路后集中检查。正文缓存沿其原格式／投影有效性合同失效并只重建可再生缓存；阅读记录的 projection_version 不充当缓存版本或历史 source_view_revision，不删除阅读记录与草稿。
5. 本变更不重排 canonical 业务数据；为结构化内部消息写入原 `message_projections.response_outline_json` 轻量 Part 轮廓，Rollout SQLite 目标 schema 为30，原消息投影版本为2，轮廓不保存思考、工具参数或结果正文，普通文本合计受原64KiB预算限制。生成协议沿原 SDK 分发。2026-11-23 核 `bun run release:policy list` 返回空列表，没有可核验的 release 支持来源；因此不为开发中的 schema29 自动扫历史补索引、不修改既有 Home，旧数据明确报告版本不匹配，待用户授权的固定来源再沿原 owner 增加必要迁移。回退代码前核对新视图格式，旧代码不能写坏新记录；如需回退使用事前保留的受支持视图原件，不加入生产双格式 reader。
6. 今后新增节点种类、普通行插入／删除、样式或组件替换而稳定坐标不变时，不提升阅读格式、不执行迁移。只有持久记录结构或 source_ref／slot／坐标含义发生不兼容变化，才由原视图 owner 评估受支持来源并登记版本及必要转换；删除一种节点不构成清理业务数据的授权。业务 Item、上下文、附件等格式仍归各自 owner，展示模块不得统一接管其迁移。
