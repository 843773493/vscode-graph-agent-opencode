## 1. 核实并固定唯一上下文读取合同

- [x] 1.1 检索所有标记、历史分页、inspection、只读文档及测试调用方，核对 System Prompt／工作区指引／Skill／runtime notice 及 user/human 角色内部注入的真实 producer、封存、派发与正文读回；包含 runtime_reminder 的打断→pending→后续消费链，在本变更 design 中记录现有能力和缺口，不以模型日志代替快照证据。
- [x] 1.2 在原 DTO／proto 中定义 Item 与 sealed contribution 的明确引用、实际消费请求与独立展示锚点（用户消息前／调用前）、版本、长度、索引已知状态和元数据 continuation；迁齐声明与直接／间接消费者并生成类型，不保留旧接口双轨，不为可用状态逐条探测正文。

## 2. 打通封存到单段正文读取

- [x] 2.1 在原 assembly／detail owner 补齐选入文本的不可变保存、精确定位和归属；复用现有内容 DAG，验证修改来源、卸载插件及重启后可读回当时版本。
- [x] 2.2 扩展原 inspection／内容读取支持冻结 contribution 引用，仅读取所选正文及必要依赖，复用原归属检查、读取限制、取消和大小边界；移除为查看一段恢复整份 assembly 的路径，不新增权限判断链、审批或通用大文件框架。
- [x] 2.3 核实 Fork 冻结 namespace 和现有清理路径不会误删仍被历史引用的正文；若有具体缺陷在原 owner 修复，覆盖缺失／损坏的明确错误，不新增 retention／垃圾回收机制，不自动修复开发数据库或回退当前配置。

## 3. 打通有界元数据历史链路

- [x] 3.1 从实际派发的 included selection 扩展现有 SQLite context marker 查询，沿现有绑定覆盖 canonical／request-only／overlay backing 文本且不重复；以原索引完成首次／版本变化／重新引入判定、重试去重及 sealed 顺序，不增加来源状态机，检查查询计划不扫描全部 assembly。
- [x] 3.2 在原历史 read snapshot 内页级批量返回标记，初始标记位于真实起点的用户消息前、打断标记位于后续新用户消息前，不被旧 execution 归属或 response_parts 容器放错位置；保持 Turn 分页、隐藏成员、无输出失败请求和活动统计，实现明确的标记元数据 continuation。
- [x] 3.3 接入实施时唯一的观察／历史失效通知，在派发证据提交后更新对应页；验证 Turn revision 未变及回答仍运行时也收到新标记、live 合并与刷新一致、多 Thread 隔离及最后观察者释放，不增加传输机制或轮询。

## 4. 打通轻量标记到原版只读编辑器

- [x] 4.1 将现有 ContextNotice 统一为主题适配的居中虚线标记，同请求且同展示锚点的来源分组；支持 transcript 起点／用户消息前的标记，不仅在模型 response 内渲染；悬停、菜单和可选来源展开只展示元数据，移除左键展开即读全文再截预览的行为。
- [x] 4.2 扩展原 Workbench 菜单／内容引用与只读文档，支持右击及 Shift+F10 后选择查看／复制冻结来源；复用原打开规则，消除预读选择方式后再次下载，UI 文案区分来源与完整请求。
- [x] 4.3 历史镜像／前端缓存仅保存标记元数据，正文由原编辑器模型管理；验证目标切换、取消、关闭、窄布局、主题及请求失败均不串内容、不写业务状态。

## 5. 定向功能与性能验收

- [x] 5.1 在原后端装具覆盖真实 producer→封存→派发→历史→内容接口：普通 internal 来源可读、included overlay backing 不漏不重、未派发不展示、A/A/B/A、重试、无输出失败、跨页、元数据续页、Fork、重启、缺失／大正文；保留原内容接口读取限制的相关回归，证明默认正文读取为零，单段读取不恢复完整请求。
- [x] 5.2 补前端定向行为回归：菜单打开／取消和来源选择零正文请求，接受查看仅目标读取一次，live→history不丢标记，已有普通 Item 菜单和原请求详情保持可用。
- [x] 5.3 必须通过 D7 的真实集成场景：页面新建→发送 U1→执行中 UI 打断并核实际取消→同 Thread 发送 U2→执行中核顶部初始标记与 U2 前打断标记→分别右键查看精确冻结正文→完成后关闭正文编辑器并刷新→关闭会话 Tab 后重开；核标记身份／相对顺序／来源／请求计数及历史一致性。使用确定性上游屏障、原业务 API 与隔离工作区，不预写 marker、不直接调用提醒 helper，不以任意标记存在或单独菜单通过替代，保留各阶段截图与原始输出。
- [x] 5.4 在原128 Turn性能装具加入大来源和大量无关历史，记录至少20个暖样本的原始时长、p50／p95／max、字节、SQL／正文读取计数；验收历史reader p95≤200ms、锚点恢复≤200ms、普通正文暖API p95≤500ms且max≤1500ms、首次编辑器正文可见≤3000ms，以及派发事实提交到标记可见暖态≤2000ms／首次≤3000ms及既有更严格门限。异常大正文单列，不删除慢样本、不无证据重跑。
- [x] 5.5 使用进程外超时与内存保护完成相关后端静态分析、真实客户端类型检查和 `bun run --cwd src/clients/web build`；核受影响源码指纹，列出真实通过／失败／未验证范围，不默认跑全仓。
- [x] 5.6 进行最小充分实现复审并检查差异，清理重复状态、第二套索引／快照、无消费者协议、兼容分支和宽泛兜底，确认无无关改动；在既有文档记录必要事实，不新增总结文档。

### 2026-11-19 验收进展

- 3.1／3.2：沿原 assembly selection 定点查询；执行计划中整份 `context_view_items` 物化由3处降为0。128 Turn 场景含360485字节来源，20个暖样本历史 reader p50=84.03ms／p95=107.99ms／max=254.61ms，未读取来源正文。装具已按真实 message→item locator 建立映射，不再混用两套序号；原失败记录保留于临时证据目录。原始数据：`out/tests/integration/backend/sessions/rollout_context/test_deterministic_rollout_history/runs/run-0f7ce2f7e51f/nodes/node-114db604ca2b/artifacts/context-history-performance.json`。
- 4.2／5.2／5.3：正式矩阵 `integration-web-context-history-markers` 1 passed／131.72秒。真实页面完成新建、U1、运行中打断并核取消、U2、首次及打断标记定位、右键／Shift+F10查看、复制及选择取消、刷新、关闭重开；正文与独立冷读及真实 wire 来源核对。普通 Item 的 Markdown 选择取消仅请求格式元数据，接受后单次下载正文。复制已迁入原 Workbench 动作 owner，删除独立 React 正文读取接线，沿同一取消及身份检查。
- 暖正文HTTP的20个原始样本 p50=50.24ms／p95=187.96ms／max=330.93ms；首次正文可见1147.61ms，后续387.48／425.63ms；首次／打断派发到标记可见的上界196.77／91.13ms。正式原件：`out/tests/integration/clients/web/itemized_context/test_context_history_markers/runs/run-43401178a085/nodes/node-35de69967917/artifacts/`；矩阵命令、完整构建、退出码和源码指纹位于同run的 `out/tests/runner/run-tests/artifacts/2026/11/19/`。
- 5.4仍未完成：上述结果不替代128 Turn浏览器 prepend／append及锚点恢复预算，尚须补齐该范围。最终静态检查仍在收口，不提前勾选对应宽项。静态／前端定向结果、失败原件与执行计划保存在 `out/tests/temp/2026/11/19/context_history_markers/artifacts/`。
- 2.3：正式矩阵 `integration-context-history-markers` 在 `run-987569b54b5e` 完整通过（6 passed／58.22秒，全部 teardown 完成）；三个模式均由真实产品消息执行生成历史，再通过产品 Fork 创建副本，修改指引并删除源 Session 后由冷 Saver 核对标记身份、历史 owner 和冻结全文。另三项验收覆盖真实派发内容接口、pending 提醒消费及元数据分页。此前阻塞来自测试装具遗漏关闭 resource platform，已在装具所有调用结束后释放原 owner，不改生产生命周期或新增清理机制。缺失／损坏及 retained detail GC 回归沿原 `test_assembly_content.py`，仍保留既有明确失败行为。
- 3.3：复用 `model.started` 与原历史 detail 读取，不增加传输或轮询。`history-realtime-regressions.log` 记录分页、消息流及统一 realtime owner 的58项通过，包含 sibling 迟到事件隔离、独立取消及最后订阅关闭；`active-call-marker-refresh.log` 定向验证已存在 Turn 的 revision 保持1时，首调用与后续新调用各触发一次 metadata/user 读取，sibling 调用变化不触发当前 Thread。运行中可见及刷新一致性由上述 D7 真实页面证明。
- 5.5：本轮14份相关 Python 文件 Ruff 通过（`static-owner-cleanup-final.log`）；Web 全量真实 `tsc -p src/clients/web/tsconfig.json --noEmit` 退出0（`web-types-active-marker-fixed.log`），修正的仅是测试常量过宽类型，不放宽业务断言。Workbench 实际 sourceTransform 类型检查退出0（`typecheck-copy-final.log`，212份源码）；正式浏览器矩阵复用与 Web build 同一正式构建入口，`run-a5f73abb8d96` 构建通过／56.42秒，浏览器随后因测试在 Gateway 身份初始化前执行打开命令失败，该失败原件保留，不视为业务通过。已改为等待真实会话树行可见再双击，后续独立运行继续核滚动行为。新测试的4项定向回归通过（`marker-owned-final.log`）；Python pyright 未安装，未声称完成该检查。
- 首次前插复核：`run-8788e89ed31b` 的20次请求均成功，但探针先改 scrollTop 后立即使用旧虚拟 DOM，19次取到错误的前置锚点；修正为等待滚动布局提交后的 `run-8dc7c16d1c7a` 中，后19次精确恢复（位移0，response→稳定布局34.19–65.11ms），首次仍有7.1875px偏移，未通过原门限。已将 scroller 外部 padding 移入原 Virtuoso Header/Footer 测量，保持原首尾留白；沿 VS Code 原聊天列表将 padding 交列表布局管理的方式处理，不新增滚动修补 owner。新实现的 Web全量类型检查退出0（`web-types-measured-spacing.log`）；ChatPanel定向7 passed／1 failed，失败项仍要求已移除的轮次下拉框，HEAD与本轮前后产品均无该select，未改测试掩盖（`chat-panel-spacing-regressions.log`）。新正式浏览器运行继续验收，不提前计为通过。
- Fork 装具清理及当前 Provider 配置合同已独立本地提交 `9d35ca31f`；未同步或push，不包含共享暂存区的其它改动。
- 来源删除回归：`run-4a5569c58c95` 的 `integration-context-history-markers` 完整6项通过（51.17秒，全部 teardown 完成）。新增断言从真实消息入口生成工作区指引来源，核对实际 Provider wire 后修改并删除 `AGENTS.md`，再次通过原正文 API 取得完全相同的冻结内容；不以当前来源文件补历史。该证据尚不替代插件卸载及真实服务重启范围，2.1继续保留未完成。
- 前插定位补充证据：`run-c6279c9204c5` 的逐帧记录显示第二批先恢复正确位置，随后 Virtuoso 重新测量使锚点偏移223.5625px。修复复用原 Turn 锚点 owner 保持前插期间的可见行，保留原布局，已撤回未证明必要的 Header/Footer 留白修改；滚轮、键盘、触摸均取消保位。相关滚动／标记定向44项通过（`anchor-marker-regressions-final.log`），真实页面复核继续进行。`run-b89a92ff12ba` 另暴露探针在分页请求已发出后重复上滚、主动取消保位，已按真实请求是否发出消除该重复手势，原失败记录保留。

- 分页复核更新：隔离复用已有128轮的 `replay-prepared-05`／`replay-prepared-08` 均取得20次真实前插；前者最大偏移0.375px、响应后布局恢复最大65.10ms。最终保留 Header/Footer 留白测量：原 scroller padding 在真实页首产生7.1875px无法补偿的偏移，将同等留白纳入原列表测量后消除；此前“已撤回”的记录仅是当时尝试，不是最终实现。滚轮驱动现在等待真实事件到达并滚到分页边界，请求开始后不再追加手势，保留全部失败原件。
- 真实 around→after 新场景发现历史窗口底部被误当作会话最新位置，跟随触发连续加载；已在原 `handleAtBottomChange` 按 `hasNewerMessages` 修正，滚动回归40项通过，Web全量类型检查与正式WorkBench构建均退出0（`bounded-window-scroller.log`／`bounded-history-web-types.log`／`bounded-history-build-02.log`）。完整双向浏览器验收继续，5.4不提前勾选。
- 最小充分复审：普通Item的格式选项仍由真实payload推导，`content_metadata`只减少响应正文传输，不宣称服务端零正文读取；上下文来源选择零预读及接受后单次读取保持。核对上游虚拟内容ref的stat size=0语义，未新增正文缓存、索引或格式推导副本。
- 取消接线补齐：Item Provider 只声明只读流读取能力，移除不可传递取消令牌的 FileReadWrite 入口；沿上游 FileService stream token 中止同一次HTTP请求。`item-documents-cancellation-04.log` 的4项检查通过（真实FileService运行中取消、Provider预取消零请求、stat零预读／字节范围及失败重读）；这不代替实际编辑器关闭时的完整页面取消验收。around窗口与分页游标已在原reducer统一替换，并清理同窗口的摘要及详情访问顺序，99项回归通过；最终浏览器双向分页仍在核实。
- 5.4 收口：`artifacts/replay-prepared-16/result.json` 复用原产品生成的128轮历史及有构建收据的正式产物，经独立 Gateway／Backend 验证20次 prepend 和20次 append。浏览器同一 Performance 时钟测得 prepend p50／p95／max=49.80／70.70／98.10ms，append=27.50／33.90／35.30ms；每项均核真实游标、响应 request_id、字节及锚点位移。旧 CDP timing 在拦截后出现负耗时，已改为同页 ResourceTiming 并要求耗时非负，不将旧负值计作通过；原件保留。返回最新及真实大纲预览 Escape 后恢复原最新窗口同时通过，页面错误为零。结合本节既有 reader、正文及派发延迟证据，5.4完成。
- 编辑器关闭复核：`run-d3d1681b621f` 的真实完整页面场景通过（1 passed／129.53秒，含54.12秒正式构建），新增场景只延迟真实 Backend 的工作区指引响应；原版关闭动作后网络请求明确中止，未返回正文或写聊天缓存。FileService定向测试先复现关闭未取消，再补原 Provider 的在途读取释放；同时核另一组仍打开时不取消。随后真实类型检查修正为原 `findEditors(resource)` 查询，避免漏掉其它编辑器类型；该最终查询仍需对应页面复核，未把前一构建当作最终版本证明。
- 最终关闭及布局证据：`marker-restart-close-final.log` 中上下文完整页面1 passed（同批另一个来源场景准备失败不计通过），`run-fa97f4d966c3` 的 `closedRead.failure.errorText=net::ERR_ABORTED` 验证最终 `findEditors` 接线。`narrow-theme-markers-final.log` 随后完整1 passed／75.43秒，复用该批有收据的不可变产物，新增720px窗口和原明暗主题切换，核键盘菜单取消零正文请求并保存截图。`item-close-final.log` 的7项回归还覆盖多组关闭、目标离开后切回拒绝迟到读取、失败传播及重读；Workbench真实类型检查退出0。
- 2.1／5.1：`plugin-uninstalled-marker-recovery.log` 的真实安装／切换／移除／冷重启场景1 passed／88.03秒，卸载1.0.0和2.0.0原件后，通过真实历史及inspect API逐项核对原标记、两个版本全文及owner，不读当前包。`run-5c31fa58d3b4` 的混合来源完整页面1 passed／108.73秒，真实Composer生成五次请求，在来源修改、Skill删除和Backend generation更换后，核原标记／正文完全一致，再逐份完成五份原冻结请求的UI和冷Saver校验；复用同批正式产物并记录版本及主包指纹。此前准备失败、英文欢迎页选择器失效及旧loading文案等待失效的原件均保留，装具已迁齐当前配置键、原关闭命令和aria-busy，不放宽业务断言。`marker-dispatch-retry-final.log` 的1项定向回归补核ready未派发零标记及真实retry绑定去重，原A/A/B/A、无输出失败及零正文读取断言保持。
- 5.6：复审确认历史与缓存仅保留元数据，所选正文沿原detail与只读编辑器模型读取；新增pending仅持有在途读取的取消句柄，结束即释放，不保存正文或业务状态。未增加第二快照、索引、恢复框架、自动重放或当前来源fallback。三份受影响Python测试Ruff、三个浏览器驱动语法检查、最终实际Workbench类型检查及差异空白检查通过；同批正式构建证据沿用，未重复运行无变化长检查。原ChatPanel旧轮次下拉框测试仍为已记录的独立失败，未改断言掩盖，也不称全仓零回归。
