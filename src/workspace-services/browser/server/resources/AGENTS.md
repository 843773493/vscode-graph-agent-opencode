# 目录用途

`src/workspace-services/browser/server/resources/` 存放浏览器资源治理实现，包括共享 Chromium 运行时、内存压力观测、候选页面决策和生命周期调度。

```text
resources/
├── browserRuntimePool.js  共享 Chromium、独立 context 与运行时代际
├── browserCheckpoint.js   页面恢复状态的捕获与应用
├── memoryPressure.js      主机/cgroup 观测与压力迟滞
├── resourcePolicy.js      从快照选择冻结、回收候选的纯策略
└── resourceGovernor.js    采样、执行有界计划及失败回执
```

观测产生事实，策略产生计划，调度器调用 BrowserSession 执行；运行时池只管理 Chromium 句柄和 context。调度扫描统一调用同步的`resourcePolicySnapshot()`读取已知保护事实，不执行页面脚本；冻结前与检查点捕获后的复核调用`inspectResourceSnapshot()`检查页面并刷新保护缓存，两者不通过布尔参数或旧入口互相回退。检查点正文通过 BrowserStateStore 落盘，不由压力观测或候选策略维护。

`BrowserContextAcquisition` 返回共享 Chromium、受限句柄、独立 context 及 `chromiumInstanceGeneration`。这个代际只在当前池内每次成功启动时递增，同一实例共用；池重建从零开始，不是持久资源身份。断线回调捕获创建时的代际，Session 据此区分旧实例与当前实例；公开诊断仍使用 `generation/runtime_generation`，不得与授权版本或操作序号互换。

`resourcePolicy.js` 在构造候选前统一筛选常驻状态：热页面、冻结页面和转换中页面参与容量及阻塞归因，冷回收、丢失、关闭等非常驻记录不参与，也不解析它们的活动时间。每个候选一次组装活动时刻、空闲时长和从同一快照派生的保护理由，不另建中间候选副本。逻辑资源仍由Manager保存，策略不删除记录或维护第二份活跃索引。`nowUnixMs`、`lastActivityUnixMs` 是绝对时刻，`idleMs` 与冻结稳定阈值是持续时长；策略只比较这些值，不改变快照中的原始ISO字段。

`memoryPressure.js` 的 `memory.events` 是所属 cgroup 的累计计数，只对同一目录的连续有效观测求增量；换组、观测缺失或取消限额后，下一次有效观测仅建立基线，不补报历史 OOM。主机比例、当前 cgroup 比例仍参与当轮压力分类。`*UnixMs` 是同一注入时钟的绝对毫秒时刻，`*StableMs` 是降级所需稳定时长；对外 `sampled_at` 保持 ISO 观测时间。

动作失败的退避期限由 `resourceGovernor.js` 持有，值为下次允许动作的Unix毫秒时刻，不是持续时间。每轮先按Manager当前治理列表及到期时间清理记录，再读取资源快照；退出治理或到期的记录不能等快照成功才释放。最近失败历史有自己的有界列表，不随退避清理而删除；策略只消费当前有效的退避保护，不另存第二份期限。

调度器注入的`now`返回Unix毫秒，动作、执行批次与整轮分别记录`actionStartedAtUnixMs`、`batchStartedAtUnixMs`和`cycleStartedAtUnixMs`；错误观察时间为`occurredAtUnixMs`。公开`started_at/occurred_at`仍为ISO，`duration_ms`为时差；性能测试的`scanStartedAtPerformanceMs`则属于测试进程的performance时钟，不与治理时钟混算。`ResourceBatchLimits.maxDurationMs`是动作块之间检查的软预算，不取消或伪报在途动作；`backlogIntervalMs`只表示仍有积压时下一轮的等待，普通/压力轮询与整批失败退避各自使用对应间隔，不另设“动作后等待”别名。

`browserCheckpoint.js` 在一次同步页面采集内按父节点索引兄弟元素的同类总数与顺序，避免匿名表单为每个控件重复扫描整组兄弟；索引不跨采集保留。定位符继续使用原ID转义与同类型序号规则，不能为了减少扫描改变恢复目标。

检查点顶层`created_at`是页面采集后、读取context storage前的Node UTC ISO组装时刻；`pages[].created_at`保留原页面创建事实。两者都不是检查点落盘完成时间，顺序采集也不提供跨页面与storage的原子时刻保证。

# 可修改内容

- 可以调整资源压力阈值、迟滞策略、冻结/唤醒/回收候选排序。
- 可以维护 Chromium 运行时共享和空闲释放逻辑。
- 可以新增资源治理的单元测试。

# 不可修改内容

- 不在此目录实现 HTTP/WebSocket 路由。
- 不在此目录实现 attach 页面 UI 或 Agent 工具。
- 不直接读写工作区业务数据，持久化必须经由 BrowserStateStore。

# 规范

- 使用 ESM，通过组合将策略与 BrowserSession 执行逻辑分离。
- 压力观测失败必须暴露详细错误，不得将读取失败视为“内存充足”。
- 运行时事件绑定创建时的 browser 句柄与 `chromiumInstanceGeneration`；旧实例的迟到断线只能通知旧代际，不得清空当前实例或其 context。
- 运行时池同时统计正在获取和已经持有的 context；空闲回收不能抢占创建中的 context。关闭共用一个在途 Promise，新获取先等关闭完成，创建失败仍按空闲策略释放运行时。
- 关闭成功或收到所属实例的断线事件后，才能清空其 browser/context 归属。关闭失败原样抛错，保留仍存活的句柄并撤销本次预期断线标记；后续异常断线仍须通知对应代际，不能把失败伪装成已关闭。
- 关闭请求同时覆盖当前在途启动，启动失败须向获取方与关闭方原样报告；旧实例未完成的context获取不能在关闭后成功交付，迟到结果须释放，不能混入新实例。关闭期间不再安排空闲计时，关闭完成后的新获取仍可启动下一代运行时。
- 自动回收必须按压力等级使用有界批次；每批生成去重计划，限制动作数、并发数和执行时间，批次结束后重新采样。
- 单个浏览器的冻结或冷回收失败不得停止整个资源调度器，但内存压力观测失败必须显式停止并暴露错误。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
