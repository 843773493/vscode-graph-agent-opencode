## 1. 核对原合同与迁齐范围

- [x] 1.1 全仓检索过程投影、final 判定、阅读状态、Turn-only 导航、child 编辑器、缓存序列化和相关测试消费者，确定唯一展示态／阅读位置入口及原业务 owner 边界与需物理删除的旧分支；保留共享 worktree 其它任务改动。
- [x] 1.2 核对 release 支持基线与原视图格式，确认原 projection_version=1、目标=2；缺基线时记录缺口，不推断开发数据迁移合同，不自动清除或重写既有记录。
- [x] 1.3 核对 Kunlun 三层 v1 和当前 Workbench 悬停、焦点、虚拟列表设施；明确原 tool_details_expanded 的实际含义，避免将原始字段显示开关误作第三层折叠状态。

## 2. 贯通原历史窗口与逐层读取

- [x] 2.1 在原 history schema／proto 增加互斥的 anchor_item_id、响应 located_item 与 activity_outline，详情请求成对回传原 source_view_id／source_view_revision 并在同一 snapshot 校验；将 execution_item_ids 统一为最多四项的 item_ids，迁齐服务校验、SDK／客户端生成类型及全部调用方，不留旧字段别名。
- [x] 2.2 在原 reader 的同一 snapshot 内复用 Item metadata 核验，完成普通 Item 的 Turn window 与无 Turn Item 的逻辑邻近窗口；扩展原复合 cursor 冻结 Item anchor，保留两路上界、方向、include、revision 和耗尽语义。
- [x] 2.3 在原 mapping／索引查询增加有界活动轮廓及真实组首坐标，不物化思考／参数／结果正文；大轮廓明确 continuation／truncation，使用首次 tail、原 before_cursor 向前续读及统一 query_mode=activity 冻结一个 process_ref；不借用 context_cursor、不改主时间线 cursor，不增加持久分组表、请求 mode 开关或第二 history API。
- [x] 2.4 贯通普通 Turn 与 execution 的选中 Item 正文回补，保留工具关联、原字节上限与正文资源入口，明确完整投影整体替换与片段经唯一 canonical reducer 按覆盖范围应用的区别，同 binding 轮廓不降级正文、旧 binding 响应不回写；定点回补最多四项且 tail 无 cursor，不推进主历史 cursor。
- [x] 2.5 仅在实际查询计划证明需要时为原存储 owner 补索引与其既有版本链；不以本次展示改造为由重建业务数据库或扫描全历史补字段。

## 3. 统一三层投影与实时终态结算

- [x] 3.1 在现有 responseParts／chatResponseGroups／transcriptItems 链路保留原协议 source 中的真实 Item／Part 和 final kind；连续思考／工具派生活动组，普通文本、压缩及通知保持边界，交错内部执行不重排。
- [x] 3.2 让普通 Turn 和内部 execution 共用三层组件与摘要规则；第一层 live 默认开、settled 默认关，仅所属 Turn／内部 execution 真实终态切阶段，attempt 或关联 Job 完成不触发收起；第二／三层默认关，零活动不创建虚假空层。
- [x] 3.3 删除最后 Markdown／最长 assistant 文本推断 final，流式未确认文本在过程内显示；原 final 确认后一次完成独立回答与终态收起，沿真实 producer／内容部位／canonical source 重绑定临时和持久 Part，不要求 part_id 相等；final 聚合或缺映射时不文本猜测，明确未精确保位，避免重复回答及摘要晚出现。
- [x] 3.4 将第一层实时／历史分支和各项重复折叠状态迁到唯一展示 owner，保留阶段与逐项稀疏选择；虚拟化重挂载、终态重放和详情回补不覆盖用户选择。
- [x] 3.5 接通按层缺失投影请求与已有详情缓存；关闭期间的迟到响应只补缓存，不重新展开，第一／二层不提前读取大型正文或运行完整 Markdown 解析。

## 4. 统一视图记录、锚点与导航

- [x] 4.1 在 SessionViewState 与原 IndexedDB 入口实现版本2的单一 reading_anchor（node_key、content_position、viewport_offset_px）、follow_latest 与折叠覆盖；viewport_offset_px 表示有符号 CSS 像素差；node_key=source_ref＋slot，不含阶段或展示父链；统一真实 Thread scope 与 main 别名，不新增数据库、窗口注册表或原生存储桥。
- [x] 4.2 在原 transcript 投影及组件接入稳定来源＋展示部位、可见祖先和测量合同，统一消息、标题、附加数据虚线及独立卡片，slot 不混用消息 role，节点键不随 final 父容器或 live／settled 阶段变化；虚线不以完整子项 ID 拼接作为身份，装饰／骨架不创建持久锚点。未提交 Part 仅临时保位，持久保存真实过程标题。
- [x] 4.3 统一先恢复折叠再有界加载／挂载／定位的流程，覆盖手动折叠、终态自动收起、行增删、prepend／append、图片和宽度重排；布局变化继续持有原节点而非重选首项，区分用户滚动与程序补偿，跟随最新与节点锁定互斥，几何极限只一次合法钳制，不垫空白，避免保位循环。
- [x] 4.4 迁齐 Turn 跳转、搜索、工具结果、引用、child Thread 和内部 execution 导航，移除 TurnItemNavigation 对 turn_id 的强制要求；显式定位只展开必要祖先且可重复定位，旧请求不跨 owner 生效。
- [x] 4.5 在同一入口交付精确目标／可见祖先、未加载／挂载、已确认不可用和实际失败的结果；DOM 缺失不当删除，只有普通恢复已确认不可用才有界使用同 Thread 相邻目标，显式跳转不冒充精确成功。超时、归属／权限、版本、格式及存储错误保留原原因／request_id，不扫描、吞错或清库。
- [x] 4.6 同 Thread 多编辑器保持独立运行位置，由最后实际交互保存持久默认；存储通知不拉动其它编辑器，fork 不继承来源记录，用户在 Composer 输入时自动收起不抢焦点。
- [x] 4.7 将未来节点扩展限定为静态接入来源／部位、祖先、原加载归属和测量合同，不新建注册服务／节点目录。迁移只在持久结构或坐标语义不兼容时由原视图 owner 按支持来源执行；普通新增节点、行增删和样式调整不自动提升版本或接管业务迁移。

## 5. 对齐 Workbench 展示与状态语义

- [x] 5.1 使用原主题变量、字体、图标、菜单与结构化悬停，摘要单行且箭头常在右端；收窄时依次隐藏附加用量、活动描述和 Item 数，保住操作、状态与可信耗时。
- [x] 5.2 使用真实仍运行事件及本地化工具目录表示当前活动；保留单项错误、整轮 failure、中断和未知结果区别，不新增审批或因失败强制展开。
- [x] 5.3 沿原权威计时／Item 计量与实际请求 usage，仅允许缺省统计无来源时省略数字，历史必填时间缺失／倒退／revision 错误仍显式失败；不累加并行耗时、不混合回退模型容量；运行态展开时不重复转圈。
- [x] 5.4 核对用户留言、必要恢复操作、附件／产物与上下文通知入口在各层折叠、终态和键盘操作下始终可达，焦点只从被隐藏子树转回标题。

## 6. 集中完成定向验证

- [x] 6.1 为稳定合同补少量定向测试：真实 final 与长中间文本、交错回执、组中段补页身份及远处 Item 无需遍历轮廓即可定点挂载、临时／canonical Part ID 不同的提交关联、跨阶段／父容器节点身份、attempt／Job 提前完成不收起、零活动／encrypted-only、回退／中断，以及虚线补读成员不改 key、未挂载不当删除、加载超时不走相邻替代；不对高频计时或外观文字写镜像测试。
- [x] 6.2 经正式 API 验证 Turn／Item around、零 Turn 和超过256条内部记录、普通 Turn／execution 轮廓 tail→before 续页、局部四项回补保留其它成员、同 binding 轮廓不降级正文、四项回补上限、错误 owner／revision 拒绝及 cursor 不受详情补读影响，核实际序列化响应和字节。
- [x] 6.3 完成相关 Python 静态检查、真实前端类型检查与 `bun run --cwd src/clients/web build`，测试一律使用原 test runner 或外部 timeout／内存保护；源码发生并发变化时仅补验受影响部分。
- [x] 6.4 在复制 fixture 的隔离工作区，经真实 Workbench → Gateway → Workspace 浏览器入口验收普通／子 Thread、内部执行、三层开关、跳转／刷新、自动收起焦点及同 Thread 双编辑器不互拉；核上方插入虚线／卡片、补读增高、首尾几何极限、移除上方节点及下方追加时原阅读对象与屏幕位置保持。
- [x] 6.5 在实际浏览器验收六会话同屏、极窄拖拽、宽度重排、长 Markdown／大型工具结果和上方内容增高，核箭头、信息隐藏顺序、Composer 可用、可见目标身份与无持续布局跳动；保存必要截图供复查。
- [x] 6.6 复用128 Turn fixture记录至少20次暖原始样本和首次读取，核 history p95≤200ms、分页／位置恢复及有数据开关单次≤200ms、网络补读展开p95≤500ms且单次≤1500ms、首次≤3000ms；保留命令、源码指纹、p50/p95/max、request_id、字节、退出码与未验证范围，不重跑筛样本或扩大为全仓性能测试。第二轮 history p95=228.6ms 未达标的原件保留；第三轮按产品凭据缓存语义修正暖采样后，run-4da87d413579 完整通过，history p95=145.8ms，门槛未放宽。

## 7. 删除旧链路与最小充分复审

- [x] 7.1 迁齐所有直接／间接调用方和测试后，物理删除实时／历史两套过程摘要、末段文本 final 猜测、Turn 内长距离像素恢复、execution-only 回补旧字段及同义折叠状态，不保留旧名转发或隐式 fallback。
- [x] 7.2 从原代码重新推导最小实现，删除本次无必要的包装器、重复状态树／缓存／校验、注册框架、定时重试与额外持久化；核滚动算法不逐业务类型堆分支，资源事实仍归原 owner，真实故障不被统一入口吞成“已删除”，缓存／IndexedDB 故障透明而不清库。
- [x] 7.3 核对本变更 delta 与已存在 failure-reporting 等规格的一致性，只标记已有真实证据的任务完成；明确未完成验收与支持来源缺口，不替其它任务勾选通过或擅自同步／重启 dev。

## 验收证据与范围

- dev 集成时用户明确授权将现有 rollout v29 无损升级到 v30；补齐显式迁移，复用原维护 owner 的锁、SQLite 备份、事务、失败恢复和原消息 codec。两个工作区共4份数据库、60条消息迁移完成，原 canonical JSONL 摘要及消息事实逐库保持一致；原 v29 备份留在各 Thread rollout 目录。新库与迁移共用唯一消息投影 DDL，必填字段无伪造默认值；正常读取不增加旧格式回退或自动迁移。定向事务及迁移检查10项通过，静态检查通过；命令与原件记录见 `out/tests/temp/2026/11/23/commit-sync-dev-release/artifacts/`。此授权仅登记已确认的 v29 来源，不补造 release 验收基线。
- 第三轮收尾复审：虚线分组直接复用阅读节点键，删除仍含 before-item／ordinal 的旧分组键；同一消费请求的页外旧元数据与输出后的新元数据合为一个节点，并优先使用已确定的真实输出位置。定向24项通过，新增场景同时覆盖两种补页到达顺序、节点身份及跨 Turn 的插入位置；不增加状态、兼容分支或持久化。最终包内真实 tsc、完整 Workbench 构建与严格规格检查通过；随后仅修正测试的可空类型断言并回归8项，产品源码未再变化。收尾命令、失败原件及源码指纹见本轮 artifacts/verification.json。
- 2026-11-23 第三轮补充审查：已有 execution 分段标题不因隐藏正文释放而补读；已被真实分段替换的空执行标题明确不可用。上下文标记节点固定为 model_call_id＋anchor 类型，before-item／ordinal 只决定插入位置；按原 history 的 anchor_context 在同一 snapshot 定点读取，不扫描128项元数据续页，context_cursor 保持该请求／类型选择。后端确认不可用时返回带来源的404，前端结束恢复且不污染整条会话；同 binding 常规详情不撤销已补读标记，新 binding 不继承它们。本次完成当前未发布的阅读格式2，不为开发中的三元标记键保留别名／推测转换，原不支持记录仍保留并明确报告。
- 第三轮真实浏览器 run `run-4da87d413579` 完整退出0；20次暖 history p50/p95/max=122.5/145.8/156.4ms，缓存开关max75.5ms，网络展开p95/max=115.7/178.5ms，prepend/append max=78.4/45.2ms，首读683.0ms，零页面错误，保留六会话／六编辑器截图。计时归因证据：原测试每个样本额外读取本地凭据，产品 getGatewayToken 则缓存；第三轮只将一次凭据准备移到暖采样前，仍计完整 Gateway 请求、正文／JSON 解析和浏览器往返。该 run 早于随后上下文定点恢复修复，后者由真实后端 API、前端导航／reducer定向检查及最终类型／完整构建覆盖，不宣称其浏览器交互已新增验收。该 run 的凭据准备时长因结果对象整体替换未写入最终JSON，后续测试已保留此字段；未为补统计重跑。
- 第三轮最终前端7文件122项通过；真实上下文历史API4项通过，覆盖首请求133项、同Turn第二次消费定点查询、独立context continuation及错误目标404，新增测试两次因装具未接生产异常出口／TraceMiddleware而失败的原件均保留，修复装具后通过。Web真实类型、Workbench转换源码类型、最终完整构建及严格OpenSpec检查通过。首次全文件的3个fork场景因要求正式matrix runner而未进入业务，不计通过；未无依据重跑。全局OpenAPI导出仍被其它变更的StreamFailure缺terminal_commit_id／terminal_outcome阻断，未绕过导出器手改离线镜像；本链路live history descriptor单独核对。Buf breaking只报已授权execution_item_ids→item_ids字段更名，未新增其它删除，原旧名调用方检索为空。命令、源指纹、失败原件与退出码见 `out/tests/temp/2026/11/23/conversation-process-navigation-review-3/artifacts/`。未同步或重启dev。
- 2026-11-23 第二轮补充审查：规范 Item URI 无论有无 Turn 都交原 Thread 定位，移除自动改开原文的分支；早期 execution 分段按原 slot 中真实段首 Item around 补读，不以同 execution 的尾段、独立回报或空标题冒充已加载。ThreadInput 推迟到全部准入通过后创建，避免 Item 核验／迟到身份拒绝遗留工作区订阅；主 Thread 解析后的身份也在进一步读取前核验。未新增缓存、坐标字段、注册器或迁移分支。
- 本轮会话定向84项通过；Opener Chromium合同45项通过，后续订阅释放改动另以7项定向回归通过；最终导航35项通过。最终 Web／Workbench 真实类型检查及完整构建通过。初次测试装具遗漏 provenance 和使用错误 execution 状态的类型失败及修复记录均保留于 `out/tests/temp/2026/11/23/conversation-process-navigation-review-2/artifacts/`，不计作产品故障。
- 本轮真实浏览器 run `run-c4579e045f48` 的页面驱动退出0，完成普通／子 Thread、无 Turn、手动关闭显式定位后重挂及同目标再跳转、刷新、六会话／六编辑器和248px窄布局，零页面错误；后续 pytest 性能门失败，整条测试退出1，不计为全通过。20次 history p50/p95/max=157.7/228.6/247.5ms；缓存开关max49.4ms，网络补读展开p95/max=317.1/929.4ms，首读469.3ms。原始请求身份／字节、样本及两张截图保留在正式测试 run 的 `node-651e63c613e8/artifacts/`。该 history采样直接调用API，不经过本轮修改的导航／Opener；归因尚未完成，不宣称偶发或由本轮引入，未重跑筛样本。浏览器构建早于最后的 Opener订阅释放修改，后者由上述7项合同、最终类型和构建覆盖。
- 2026-11-23 补充审查：修复同一 Item 多 Part 的折叠祖先误匹配、上下文／临时节点来源坐标不完整匹配、显式定位残留重开用户折叠、压缩正文漏接定位、final 定位误开过程及无 Turn 独立回报等待不存在的 body；定位取消保留已取得的布局锚点。流式 text→final_text 只在真实 producer／block 唯一匹配时重绑定，聚合 final 缺少映射时仍按标题结算，不按正文猜测。
- 本轮最终定向前端81项通过，真实类型检查与完整 Workbench 构建通过；命令、原始输出和最终源码指纹保留于 `out/tests/temp/2026/11/23/conversation-process-navigation-review/artifacts/`。初次新增测试的选择器／DTO类型错误及其后修复记录均保留。
- 本轮新增浏览器回归验证“显式定位→手动收起→父层卸载重挂→相同目标再跳转”。正式 run `run-5c4f9f8b1f69` 在启动 Gateway 时因测试端口38132占用而失败，尚未进入页面，不能计为本轮浏览器通过；未终止占用进程、未放宽断言、未直接重复长跑。最后的来源坐标与 notice 小修已补定向检查和构建；下列前轮浏览器记录仅证明其对应源码版本。

- 2026-11-23：真实浏览器 `test_rollout_history_around_loading_real_web_chain`，run `run-1a199014055d`，退出码0。正式结果及两张六编辑器截图位于 `out/tests/integration/clients/web/test_rollout_history_progressive_loading/runs/run-1a199014055d/nodes/node-651e63c613e8/artifacts/`；覆盖静态 canonical 历史、真实 fork、普通／子 Thread、无 Turn 执行、阅读恢复及真实拖拽，不声称执行了上游真实模型。
- 本轮各20次原始样本：history p50/p95/max=144/169/176ms；缓存开关=38/45/49ms；网络补读展开=110/369/446ms；prepend 合并与保位=51/68/70ms，append=34/41/44ms；首读501ms。原始小数、request_id、字节及前序失败记录保留，不以本轮通过宣称所有启动样本都达标。
- live 终态自动收起不抢 Composer 焦点已有 `run-defcece39e21` 的独立观察证据；其 basic 综合场景后来因 pending-requests 重读超时失败，不计为整条 basic 通过。边界展示与未知结果的定向浏览器验收分别为 `run-5563eddb8f7b`、`run-8b3881c1cb1a`。
- 后端聚焦检查 `backend-25.log` 107项通过；内部执行与预算收尾 `backend-37.log` 87项通过；前端相关162项通过、后续定点47项通过，真实类型检查与完整 Workbench 构建通过。命令、输出及源码指纹保留于 `out/tests/temp/2026/11/23/conversation-process-navigation/artifacts/`。
- 全 OpenAPI 总检查仍存在其它变更的 `message.v1.StreamFailure` 描述缺字段问题，本变更 history descriptor 已单独验证。无可核验 release 支持基线，不自动迁移开发中的旧 schema／阅读记录；未同步或重启 dev，未代其它变更勾选完成。
