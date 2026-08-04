## ADDED Requirements

### Requirement: 未索引超大旧 Trace 必须遵守同步字节预算

当旧会话没有 append-time 轻量索引，且最新语义 Trace 行超过 bootstrap 同步读取预算时，系统 MUST 先返回 `partial` shell 并异步迁移，不得同步解析完整大行。迁移完成后必须发布最新完整 Turn；已有轻量索引的会话仍必须 latest-first。

#### Scenario: 超大 legacy 行先返回 partial shell

- **WHEN** 用户打开没有轻量索引且尾行超过同步字节预算的旧会话
- **THEN** bootstrap 不读取该完整大行，返回 Composer 可用的 partial shell，并在后台迁移完成后提供最新 Turn

### Requirement: Job 是权威 Turn 边界
系统 SHALL 使用一次实际执行的 Job 表示一个 Turn，并在 Turn 中保存所有来源消息 ID 和被合并 Job ID；分页结果不得拆分 Turn 的用户输入、工具活动和最终状态。

#### Scenario: 普通 Job 形成一个 Turn
- **WHEN** 一个用户消息启动并完成一个 Job
- **THEN** 历史中出现一个以该执行 Job ID 标识的完整 Turn

#### Scenario: Steering Job 被合并
- **WHEN** 多个 steering 消息被合并为一次实际执行
- **THEN** 系统返回一个执行 Turn，并列出全部来源消息和被合并 Job ID

### Requirement: 会话 bootstrap 有界且最新 Turn 优先
系统 SHALL 提供大小有界的 bootstrap，返回会话 shell、最新 Turn summary、活动 Job 摘要、历史 cursor、事件 cursor 和投影 epoch；该读取 MUST NOT 扫描完整 checkpoint 或完整 Trace。

Turn 的 summary/header MUST 具有固定读取上限，并与完整 detail 在同一次原子文件替换中发布；bootstrap、summary 分页和无 pending WAL 的常规恢复 MUST NOT 读取完整 detail。SSE transport cursor MUST 与业务 event ID 分离，并支持对任意已发送事件进行 O(1) byte-range 恢复；未命中有界语义索引的旧 raw event ID MUST 明确失效，不得回退扫描完整 Trace。

#### Scenario: 切换长会话
- **WHEN** 客户端请求拥有大量历史和 Trace 的会话 bootstrap
- **THEN** 响应读取量只与固定 bootstrap 上限和最新活动状态相关，并首先标识最新 Turn

### Requirement: Turn 支持 summary 分页和详情水合
系统 SHALL 以完整 Turn 为单位倒序分页 summary，并 SHALL 支持按 Turn ID 批量获取 full detail；单页条数和详情批量数 MUST 受上限约束。

#### Scenario: 向前加载历史
- **WHEN** 客户端携带 older cursor 请求下一页
- **THEN** 返回游标锚点之前的完整 Turn，且不会重复或拆分锚点 Turn

#### Scenario: 水合可视 Turn
- **WHEN** 客户端请求一组可视 Turn 的 full detail
- **THEN** 系统返回这些 Turn 当前 revision 对应的完整展示数据

### Requirement: 游标不受普通追加与压缩破坏
系统 SHALL 使用稳定 Turn 锚点和投影 epoch 生成 cursor。新增 Turn、Turn 终态更新、checkpoint 写入及上下文压缩 MUST NOT 使已有 cursor 失效。

#### Scenario: 分页期间新增 Job
- **WHEN** 客户端持有 older cursor 且会话随后新增 Turn
- **THEN** 旧 cursor 仍返回原锚点之前的正确历史

#### Scenario: 分页期间发生上下文压缩
- **WHEN** checkpoint 因自动压缩而变化
- **THEN** 已签发的 Turn cursor 继续有效且 Turn 顺序不变

### Requirement: 破坏性历史变更显式失效 cursor
系统 SHALL 在删除、回滚或无法保持 Turn 身份的重建发生时递增投影 epoch，并 SHALL 对旧 epoch cursor 返回明确错误。

#### Scenario: 回滚后使用旧 cursor
- **WHEN** 客户端在历史回滚后使用旧投影 epoch 的 cursor
- **THEN** API 返回可识别的 stale-cursor 错误，而不是静默返回其他页面

### Requirement: Replay 同步回退模型上下文与 Turn 投影

编辑并从此处继续、重新生成和失败重试 SHALL 以目标用户消息为边界同步截断模型 checkpoint 与 Turn 展示投影，并 SHALL 在 dispatch 新 Job 前原子隐藏目标 Turn 及其后缀。工作区文件修改 MUST NOT 被该操作撤销。

#### Scenario: 编辑旧消息后继续

- **WHEN** 用户编辑一个已有用户消息并确认从此处继续
- **THEN** 新模型请求只包含目标消息之前的上下文和替换消息，聊天时间线不再展示目标 Turn 及其后续旧分支，并开始显示新的执行 Turn

#### Scenario: 重建包含 replay 的历史

- **WHEN** 系统从持久 Trace destructive rebuild 一个包含 replay 元数据的会话
- **THEN** 重建投影继续隐藏被替换 Turn 及其旧后缀，不得使旧分支重新出现

### Requirement: 破坏性投影发布拒绝过期 staging

系统 SHALL 在同一投影锁内原子捕获 `projection_epoch`、事件 ID 与 source offset 作为 staging 发布基线，并 SHALL 在发布前同时校验三者。任一基线发生变化时，旧 staging MUST 明确冲突并放弃发布。

#### Scenario: Migration staging 与 replay 使用相同事件水位

- **WHEN** migration 捕获 staging 基线后，replay 在事件 ID 和 source offset 不变的情况下发布了更高投影 epoch
- **THEN** migration staging 因 epoch 不匹配被拒绝，不得覆盖 replay 投影或复活旧分支

### Requirement: 长历史 replay 遵守 detail 内存边界

系统 SHALL 使用有界 summary/header 分页定位 replay 目标。隐藏大量 Turn 时 MUST 只读取和改写有界 header，并以有界缓冲流式复制 detail；系统 MUST NOT 为一次回退同时反序列化全部后缀 Turn 的正文、工具事件或 Trace items。

#### Scenario: 编辑长会话的早期消息

- **WHEN** 目标消息之后存在大量包含大型 Markdown 和工具 Trace 的 Turn
- **THEN** replay 的内存占用不与全部后缀 detail 总量线性增长，且 bootstrap、分页和常规 SSE 的读取边界保持不变

#### Scenario: 实时 replay 已完成显式截断

- **WHEN** 新 replay Job 携带的截断 epoch 等于当前权威投影 epoch
- **THEN** 增量 projector 不再重复扫描已隐藏的历史，而 destructive rebuild 仍能依据 replay 元数据恢复隐藏关系

### Requirement: Turn 更新可幂等合并
每个 Turn SHALL 具有单调递增 revision，bootstrap、分页、详情、终态协调与 SSE SHALL 共享相同的 Turn 身份和 revision 语义。

#### Scenario: SSE 先于 bootstrap 到达
- **WHEN** 客户端先收到较高 revision 的 SSE 更新，随后收到较低 revision 的 bootstrap 数据
- **THEN** 较低 revision 不得覆盖当前 Turn

### Requirement: 投影可恢复且失败透明
Turn 投影 SHALL 在会话节点内持久化语义操作、水位和索引，并 SHALL 使用事件 ID 幂等恢复；检测到无法恢复的不一致时 MUST 返回包含原因的错误。

Trace append SHALL 在区分语义索引事件与非索引事件之前、同一会话文件锁内恢复未完成的索引事务；恢复后的首个事件即使不进入语义索引，也不得占用 pending 事件的 Trace 区间或破坏后续索引写入。

#### Scenario: 进程在写入后重启
- **WHEN** 投影操作已落盘但 manifest 更新前进程退出
- **THEN** 恢复过程幂等重放该操作且不产生重复 Turn

#### Scenario: Trace 终态落盘前进程退出
- **WHEN** Trace 已有 `job_created` 或最终 `text_end`，checkpoint 已持久化 assistant 最终回答，但真正的 Job 终态事件尚未写入
- **THEN** 旧会话迁移把 checkpoint 回答补到同一实际执行 Turn 并将其置为完成，且已有失败、取消或完成终态不得被覆盖

#### Scenario: 首次访问前已有旧 checkpoint 与新 Trace
- **WHEN** 升级后的会话在首次建立 Turn 投影前已产生一个新 Trace Job，同时 checkpoint 仍含更早历史
- **THEN** bootstrap 返回 partial 并触发迁移，迁移完成后旧 checkpoint Turn 与新 Trace Turn 均存在
