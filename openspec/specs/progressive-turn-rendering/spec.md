# progressive-turn-rendering Specification

## Purpose
TBD - created by archiving change optimize-long-session-turn-loading. Update Purpose after archive.
## Requirements
### Requirement: 最新 Turn 先于旧历史完整呈现

客户端 SHALL 在会话切换时先呈现最新 Turn summary；用户消息的可见文本和附件卡片元数据可以随 summary 一起显示，完整中间消息、原始 provider block 和完整附件正文只在用户明确请求或点击附件资源后按需请求，历史页和完整 Trace 不得阻塞这一过程。新用户消息的展示文本 SHALL 从结构化 block projection 得到，不读取或复制 `display_content`；内部消息仍按既有 metadata policy 处理。

#### Scenario: 切换到长会话

- **WHEN** bootstrap 返回最新 Turn summary
- **THEN** 最新用户输入、附件预览占位/缩略图、最终响应和活动统计先显示，完整中间消息保持未加载，直到用户明确展开该 Turn

#### Scenario: 用户消息包含模型预览 base64

- **WHEN** 最新用户消息的 checkpoint content 包含 `image_url` data URL
- **THEN** 客户端只使用附件 metadata 请求缩略图并渲染附件卡片，Composer/用户消息正文不渲染原始 data URL 或 JSON block

### Requirement: Markdown 渐进且非阻塞渲染
客户端 SHALL 先提交轻量文本或稳定骨架，超过固定阈值的 Turn detail JSON SHALL 在主线程外解码和解析，再以低优先级增强 Markdown；大型详情处理和 Markdown 解析不得阻塞 Composer 输入。

#### Scenario: 最新回答包含大型 Markdown
- **WHEN** 最新 Turn full detail 包含大量表格、代码块和列表
- **THEN** Composer 在 Markdown 增强期间保持响应，先展示有界格式化预览，并在用户明确展开后展示完整格式化内容

### Requirement: 仅水合可视范围详情

客户端 SHALL 使用虚拟列表，且只为可视内容及受限 overscan 取得当前折叠层级所需投影。可见过程不等于全部工具正文可见：第一层只需中间文本与轮廓，第二层只需组内轻量成员，第三层才水合选中详情。折叠的大型 reasoning 与工具输出在展开前 MUST NOT 执行完整 Markdown 解析；已加载数据可按既有缓存上限复用，不因隐藏仍持续挂载大型 DOM。

#### Scenario: 快速滚动长历史
- **WHEN** 用户滚动包含大量 Turn 和内部执行的会话
- **THEN** DOM 和详情请求量与可视窗口、折叠层级和固定 overscan 相关，不随全部历史线性增长

#### Scenario: 第一层展开但工具详情关闭
- **WHEN** 可视过程的第一、二层展开而所有工具第三层收起
- **THEN** 仅挂载轮廓及轻量标题，不解析或挂载全部大型工具结果

### Requirement: 历史分页和布局变化保持阅读锚点

客户端 SHALL 保留普通 Turn 分页边界与内部 execution 的原有界分页，并在前插、后插、实时更新、折叠与布局变化时按稳定展示节点、节点内位置和视口相对位置保持阅读目标。分页、统一实时通道与终态协调 MUST 使用原 canonical upsert 合并，不能替换全部历史。位置采集 SHALL 选择与实际视口阅读基准相交或紧邻的独立语义节点，忽略装饰和临时加载提示，不选取视口下面尚不可见的 Turn。布局更新期间 SHALL 持有原稳定目标，而不是每次 DOM 或高度变化都重选首个可见项；用户主动滚动或明确改变阅读目标后才重新采集。跟随最新时只执行尾部跟随，不同时锁定节点；程序保位与恢复不得误判为用户滚动或改变 follow_latest。若内容不足一屏或目标位于首尾导致原视口偏移不可达到，只做一次合法滚动钳制，不垫空白、不无限重试，不判作目标丢失。

#### Scenario: 顶部加载旧 Turn
- **WHEN** 用户滚动到顶部加载前一页，当前阅读对象是组标题或某个工具详情
- **THEN** 原阅读对象及目标内位置保持，新页保留原 Turn 和内部执行边界，不依赖旧虚拟索引

#### Scenario: 历史加载后所属过程进入终态
- **WHEN** 已加载多页历史且所属 Turn 或内部 execution 进入真实终态
- **THEN** 当前过程原位结算，旧页仍保留；非跟随模式保持当前可见目标，被隐藏内容按标题规则保位

#### Scenario: 宽度变化及异步正文增高
- **WHEN** 用户拖窄编辑器、展开上方内容或图片加载改变高度
- **THEN** 相同内容身份仍作为阅读目标，不因 Turn 内像素偏移变化跳到别项，也不发生持续补偿循环

#### Scenario: 当前锚点上方新增虚线或数据卡片
- **WHEN** 用户阅读 Item B，其上方插入上下文虚线、数据卡片或补读内容使高度增加
- **THEN** 继续持有 B 的稳定展示坐标并补偿位置，新增节点不抢走锚点，不因布局更新重选第一项

#### Scenario: 删除上方节点或改变下方内容
- **WHEN** 锚点上方其它展示节点被移除，或锚点下方内容追加而用户不在跟随最新模式
- **THEN** 当前节点继续留在原屏幕位置，操作不改变其来源身份、阅读记录版本或跟随模式

#### Scenario: 首尾位置无法保持原视口偏移
- **WHEN** 布局变化后内容不足一屏，或目标过于靠近首尾，原 viewport_offset_px 超出合法滚动范围
- **THEN** 保留真实内容目标并一次钳制到可达位置，不持续补偿、不新增空白垫高、不把几何限制报告为资源丢失

#### Scenario: 历史加载后当前 Job 完成
- **WHEN** 用户阅读历史期间当前 Job 完成
- **THEN** 沿用户选择保持阅读位置，不能被终态、final 移位或新摘要抢回尾部；明确跳到最新才恢复跟随。

### Requirement: 主时间线不恢复完整 Trace
会话切换时客户端 MUST NOT 为构建聊天时间线读取完整 Trace；调试事件 SHALL 在事件视图中独立按需分页。

#### Scenario: 会话包含大量 Trace
- **WHEN** 用户只打开聊天视图并切换该会话
- **THEN** 网络请求不包含无上限的 Trace 读取，事件视图未打开时不传输完整事件历史

### Requirement: 取消终态与运行失败具有不同语义

客户端 SHALL 将用户主动 `session_interrupted` 显示为中性“已由用户中断”状态，将其他 `job_cancelled` 显示为“任务已取消”；取消事件 MUST NOT 使用运行失败文案或错误样式。只有真实 `error` 与 `job_failed` SHALL 显示为运行失败。

#### Scenario: 用户点击中断生成

- **WHEN** 同一 Turn 依次收到 `session_interrupted` 与伴随的 `job_cancelled`
- **THEN** 时间线只显示一次“已由用户中断”，不显示“运行失败”或重复取消状态

#### Scenario: 非用户触发的任务取消

- **WHEN** Turn 只有 `job_cancelled` 而没有 `session_interrupted`
- **THEN** 时间线显示“任务已取消”，不显示“已由用户中断”或“运行失败”

### Requirement: 历史体验具备可重复的 stub 验收

项目 SHALL 复用隔离 workspace 的 deterministic stub integration/component/API tests，覆盖最新窗口、Turn／Item around、before/after、三层按需加载、普通与内部执行、活动统计、慢请求和实时更新；并通过真实 Workbench → Gateway → Workspace 页面验收折叠、刷新、搜索定位、终态结算及窄宽布局。测试 SHALL 验证 Composer 可用、窗口完整、旧历史不丢失和真实 owner／revision，不要求真实模型或默认全仓长跑。不为文案、CSS 类名和每个计时 tick 编写脆弱断言。

#### Scenario: stub 历史加载验收
- **WHEN** 定向测试经正式入口执行会话切换、输入、分页和逐层详情开关
- **THEN** 请求参数、字节和可观察状态证明输入未被阻塞、正文没有提前加载、窗口及既有历史均保留

#### Scenario: 真实浏览器综合验收
- **WHEN** 在实际 Gateway 与隔离工作区打开主／子 Thread、无 Turn 执行及六个编辑器，并操作折叠、重开、跳转和拖拽宽度
- **THEN** 验证目标真实身份、可见祖先、焦点、窗口顺序和独立阅读状态，保留准确命令、原始结果、源码指纹与必要截图

### Requirement: 三层过程按需展开且不加载加密 payload

客户端 SHALL 对已结束的过程默认折叠第一层，对所有连续思考／工具组及单项详情默认折叠；运行中的第一层默认展开。摘要沿原权威耗时与 Item 计量，箭头始终靠右且按展开状态切换方向。reasoning 显示可读内容，summary 显示安全摘要，encrypted 只显示存在性提示；默认与详情响应均不得向前端交付 provider encrypted reasoning payload。展开仅读取当前 owner 和折叠层级所需受限投影，不把其它 Turn 或全部工具正文一起解析。

性能 SHALL 保留现有128 Turn 场景后端暖 history p95≤200ms及浏览器分页／恢复单次≤200ms；有数据时折叠与展开至保位后的可用界面单次≤200ms。需网络补读的普通暖展开从点击至实际目标可见可操作 p95≤500ms、单次≤1500ms，首次未缓存读取≤3000ms。排队、校验、内部请求、序列化和必要重试均计入；loading、进程 timeout、平均值或最快一次不替代终点和硬上限。

#### Scenario: 128 Turn 混合历史
- **WHEN** 复制后的确定性 fixture 混合普通 reasoning、encrypted reasoning、工具、final 和无 Turn execution，用户逐页浏览并展开一项工具
- **THEN** 已结束过程及工具详情默认折叠，不传加密 payload，仅所选正文被补读；历史、分页和恢复满足原预算

#### Scenario: 暖展开延迟与首屏
- **WHEN** 对同一数据规模执行至少20次暖轮廓／详情请求及实际展开，首次读取单列
- **THEN** 记录未筛选的原始时长、nearest-rank p50/p95/max、request_id和字节，按完整响应与可操作 UI 判定，慢样本不删除、不自动重试取绿
