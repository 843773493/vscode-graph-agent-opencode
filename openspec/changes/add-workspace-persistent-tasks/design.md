## Context

动机见 `proposal.md`。当前 Goal 由 `SessionGoalService`、`GoalRuntimeService` 与 `goal.json` 管理，按 Job/Agent 事件、API 激活和启动恢复驱动主线程；续跑经 `submit_runtime_event` 进入 `RuntimeSourceDispatcher`。该通用入口目前联动提交内部 source 与 durable wakeup，不能把它描述成已有完整的“仅注入”公开能力。

现有定时会话生成由 Gateway 的 `SessionGeneratorStore`、`SessionGeneratorScheduler`、`SessionGeneratorCoordinator` 持有定义、调度和协调；工作区 `SessionGenerationService` 执行会话策略。前端虽然按工作区筛选展示，实际 owner 仍在 Gateway。本变更将这部分归属迁回工作区。

相关合同：`add-context-injection-lifecycle` 规定唯一上下文 mutation owner、pending/applied 和内部 source；`add-itemized-rollout-context` 规定 thread identity、准入、恢复与 sealed assembly；`add-workspace-persistent-resource-management` 规定 Terminal/Browser 等资源的独立 owner。本设计消费这些合同，不复制其存储或控制状态。

## Goals / Non-Goals

**Goals:**

- 用可组合、可测试的触发器、判断器、上下文构造器与动作构建持久任务，Goal 和定时任务使用同一运行时。
- 支持纯脚本条件与自然语言驱动的模型判断，未满足条件时不调用工作 thread。
- 将工作区归属、脚本执行位置、判断 thread 和工作 thread 明确分开，完整记录因果关系与等待原因。
- 在前端提供可直接使用的模板编辑和高级组件组合，以及任务、判断、投递、执行的可追踪记录。

**Non-Goals:**

- 不引入 Gateway scope、云调度器、消息队列或高可用服务；进程停止期间不执行检查。
- 不建立任意 DAG、节点画布、动态加载用户 Python/JavaScript 代码的插件宿主或第二套 Agent 执行器。
- 不扩大既有跨工作区执行能力；未支持的 locator 组合仍明确拒绝，不因本次抽象隐式放开。
- 不修改原生客户端，不接管持久外部资源的生命周期，不把 thread 空闲卸载视为任务结束。

## Decisions

### 1. 所有持久任务只有一个工作区 owner

`PersistentTask` 固定绑定 `workspace_id`。API、后台恢复和外部通知使用明确的工作区定位，不根据前端当前激活工作区重新解释已保存任务。用户创建时未指定工作区，使用现有启动配置解析出的 home 下默认工作区；既有环境覆盖继续生效，不新增另一套 home/默认路径规则。独立启动且已显式绑定工作区的后端只操作该工作区，不能为解析默认值顺带创建其他工作区。

定义、触发进度、判断结果、动作意图和运行记录持久化在该工作区 `.boxteam/` 内的统一任务存储。其内部表结构在实施时沿用仓库 SQLite owner 约定，不预先建立多个冗余 store。Session/Thread 记录保存稳定关联，thread 定位沿用 catalog，不拼接日期或导航目录。

Gateway 只负责工作区定位、托管后端生命周期与透明代理，不保存新任务定义、判断结果或调度进度，也不运行任务定时器。默认工作区以普通工作区方式承载无显式选择时创建的任务。后端启动或挂载某工作区时恢复其任务；卸载/停止时停止其调度。共享后端进程中的不同工作区不能共用未分区的任务状态。

选择工作区单 owner 而非双 scope，是为了让目录数据、脚本环境与会话业务具有相同生命周期，也消除 Gateway 与后端争抢调度权的问题。

### 2. 组件有明确类型合同，采用显式装配

任务定义包含稳定任务 ID、工作区 ID、定义 revision、模板类型和版本、触发配置、判断配置、上下文配置、动作配置与运行策略。核心决策字段使用 typed union；组件通过稳定类型与版本及配置 schema 校验，不在持久配置中存任意可执行函数，不以任意 metadata key 决定行为。

| 组件 | 输入 | 输出/职责 |
|---|---|---|
| 触发器 | 定时、明确执行的结束事件、外部事件或手动操作 | 带稳定 identity 的一次触发 |
| 判断上下文构造器 | 任务定义、触发事实、选定执行结果或资源 | 冻结的本次判断输入及来源 revision |
| 判断器 | 冻结输入 | `wait / proceed / complete / needs_attention`、理由及结构化结果 |
| 动作上下文构造器 | 任务配置、本次检查与判断结果 | 要发布的事件或状态内容 |
| 执行动作 | 明确目标、内容、投递策略 | 仅更新上下文、请求内部执行，或既有会话生成策略 |

错误、超时、无效判断输出是失败结果，不伪装成 `wait`。脚本执行位置必须显式或由所属工作区唯一确定，不让远端工作区脚本默认跑在 Gateway 主机。资源引用沿用统一资源 identity/URI，执行器负责解析，不把真实路径作为另一套长期定位机制。

初版是线性组合，不要求每个组件都变成独立服务或类。优先提取现有真实复用边界，注册和 schema 是同一组件定义的组成部分。模板是该定义的默认组合与专用表单，不保存另一份可独立演化的配置。

所有任务统一走“触发 → 判断 → 动作”。触发器回答“什么时候判断一次”，判断器回答“这次是否应该执行”。用户可以把定时任务理解为时间条件满足后执行的条件任务特例；实现仍将时间到期交给调度器，判断器只负责附加条件，不通过轮询脚本或模型判定时钟。

| 用户意图 | 触发器 | 判断器 |
|---|---|---|
| 每天九点执行 | 定时触发 | 无附加条件，始终通过 |
| 每十秒检查服务，上线后继续 | 定时触发 | 脚本判断 |
| 每十分钟分析是否适合发布 | 定时触发 | 判断 thread |
| 工作执行结束后继续 Goal | 关联执行结束事件 | Goal 状态与预算判断 |

“无附加条件”使用显式注册的始终通过判断组件，配置 schema 与其他判断组件一样验证；不是缺失配置时的隐式回退。它不启动脚本、判断 thread 或额外模型调用，本次决定仍关联原触发并可追踪。普通定时任务按每个计划点执行，不套用首次满足策略而只执行一次；增添脚本或模型条件不改变任务 ID、时间触发与已有计划游标。模板标签只用于预设和编辑展示，运行时不按“定时任务/条件任务”类型分成两套调度或执行链路。

### 3. 脚本与判断 thread 可互换，判断与工作执行分离

脚本通过约定的结构化结果声明条件是否满足，并提供提示素材。退出异常、超时、无效输出及 stderr 等诊断可查看；服务未上线是合法等待结果。配置脚本执行超时与必要资源限制，不把模型上下文容量换算成模型输出上限。

script v1的command为argv，不隐式套shell，cwd由所属工作区唯一确定。stdin以UTF-8传入任务/运行身份、冻结定义revision、触发与字面上下文，stdout以UTF-8返回四类决定、非空理由及JSON result；stdout/stderr共享输出限额。运行记录保存安全错误事实，原生stderr与无效stdout仅进已登记的原始诊断日志。执行前持久checking，检查期间释放管理锁；返回后复核revision。checking恢复时结果未知，不重跑；已有决定与动作只复用原冻结输入和接受身份。试运行的决定、失败及中断均不改变正式生命周期。

rule v1以标准JSON Pointer从同一冻结检查输入选择值，与必填expected按RFC8785规范化JSON比较；对象键顺序不影响相等，数组顺序和bool/number身份保留，数字1与1.0等价。字段缺失表示wait，不能当作JSON null；非法pointer或缺失expected在保存时拒绝。规则不启动脚本或判断模型，不读取当前文件/外部状态；result记录pointer、found、actual和expected，经同一决定事务与条件策略推进。checking中断后可从原冻结输入重算纯规则，与未知脚本结果分开；所有非always决定使用相同不可信正文动作链路。新增可选rule字段不改变已有always/script创建幂等身份，数据库仍schema18。

模型判断器使用显式绑定的独立判断 thread，由现有执行准入启动并保存本次评估与 execution 的关联。自然语言条件连同冻结材料输入判断 thread；它只返回上述判断与理由，不直接修改任务生命周期或自行投递到工作 thread。运行时校验结果后执行动作。判断 thread 的能力与工具按判断任务显式配置，不自动继承工作 thread 的执行权限。

model v1配置明确的`thread_uri`和`instruction`，使用该Thread已有独立Agent/工具选择，工作Thread配置不向其复制。判断Thread不得与动作Thread或结束事件来源Thread相同。冻结输入复用规则/脚本构造器；内部判断source经原准入提交，运行仅登记原source item、job、execution引用（Workspace schema20），不复制正文。返回JSON必须回显本次run_id和definition_revision并包含四类decision、非空reason及对象result；只从该execution最后一次ModelCall的canonical完成输出读取，工具循环的中间说明与其他执行历史不能作为结果。检查串行时后续触发保留pending；未知执行结果由原终态事件唤醒，不占用worker槽等待，也不重新分配执行。接受后硬重启先恢复原wakeup，再按原execution取结果；暂停/编辑保留已接受检查及迟到结果，原revision合同阻止动作。试运行可实际调用判断模型，但不改变正式生命周期或投递工作。

同一任务同一时刻最多一次判断在途。判断请求关联 trigger/run/evaluation identity，不能把别次历史回复当成本次结论；判断执行结束事件不匹配该任务的工作执行结束触发器，避免自我唤醒循环。重复触发的排队或合并策略显式记录，定时任务保留既有 overlap 行为，不把“判断串行”误作所有生成 Job 强制互斥。

### 4. 任务生命周期、运行阶段和应用状态分别表达

任务生命周期表达可调度、暂停、完成、终止或需要处理；当前运行阶段表达等待时间/条件、正在判断、等待目标空闲、正在投递或正在执行。后端提供权威状态和原因，前端不按无消息时长推断停滞。

Goal 的 `active/paused/blocked/usage_limited/budget_limited/complete` 及预算记账保留为 Goal 应用语义，由同一任务记录内的应用状态派生通用展示；不再同时维护可独立修改的 Goal 状态文件。一次性服务等待任务在动作获得持久接受后可结束监测；定时任务在单次执行后继续等待；需要以工作执行终态判断完成的模板则等待该终态。三者在模板策略中明确，不把 Job 完成等同任务完成。

暂停/编辑/终止通过 revision 校验与运行时检查生效：暂停后不再接受新的自动动作；已经接受的工作执行不会因暂停调度被隐式取消。迟到判断保留记录但不能用旧 revision 发起动作；需要取消在途执行时使用显式执行控制，并展示实际结果。恢复是否立即检查由触发策略明确。

### 5. 上下文发布与执行唤醒逻辑解耦，可靠组合提交

运行时调用既有上下文 owner 和执行准入能力，扩展其缺失的业务端口，不直接改 checkpoint、模型历史或 Provider 请求。

- `context_only`：提交事件或状态 observation，不创建 wakeup；下一次安全 model-call preparation 才可能消费，提交成功不能标记为模型已看到。
- `context_and_run`：持久化 source 与关联 wakeup，返回稳定投递回执；中断恢复复用同一引用与 identity，不能出现报告已续跑却没有内容的窗口。
- 会话生成动作：复用现有 Session Generation 领域能力，保留新建、继续、分支回报的既有输入输出与消息来源语义。新增脚本/Goal 内部通知不伪装成真实用户 Turn。

上下文同时支持一次性事件和同一 source 的最新状态更新。状态更新遵循已有 source revision/历史不可变合同，不覆盖已提交事实。投递时机复用既有 `after_turn / after_tool_result / after_interrupt` 语义；默认等待本轮结束，其他策略必须用户明确配置。运行中的模型请求无法中途改变，thread 的串行执行由现有 Job 准入负责。

字面动作上下文显式选择`event/state`，缺省event保持既有输入。state以task为稳定来源，正文由冻结字面内容、触发payload和判断result构造；run_id、触发键及解释理由归运行审计，不制造状态revision变化。每次发布沿原canonical source intent保留不可变接受事实；来源版本由原控制owner在事务内按内容hash及单调控制次序分配，同值复用当前版本，A→B→A取得新的A版本，不能把旧的完整帧与新的delta冒充同一版本。context_and_run在原rollout事务同时接受状态、短引用通知与wakeup。冷prepare和rebuild只从canonical版本恢复完整正文及applied基准，fork闭包保留冻结checkpoint引用的版本，不回读当前任务配置或后继来源。

### 6. 持久记录与幂等覆盖每个关键窗口

触发记录冻结 definition revision、trigger identity、输入 revision、判断引用、决定、动作 identity 和接受/执行结果。外部通知必须带幂等键；同一身份不同内容明确冲突。`first_match / rising_edge / each_match` 等条件触发策略持久保存已触发事实，首次满足不等于每次轮询都续跑；服务再次下线后上线是否重触发由策略决定。

`policies.match_mode`默认`each_match`，保持普通定时任务逐计划执行。`first_match`以同一任务已有的持久接受回执判定是否完成首次投递；`rising_edge`以原运行接受顺序中最近的有效wait/proceed判断为边沿，首次无判断等同尚未满足。干跑、过期判断、失败及complete/needs_attention不构造新的边沿，暂停/恢复和改名不重置条件事实。判断、策略选择与运行阶段同事务提交，不另建可变匹配账本。非试运行的proceed停在checked表示策略未触发动作，保留原判断和理由、无错误也无投递。已选中但尚未记录接受回执的运行恢复原动作，先对账后推进后续检查。

外部触发组件为`builtin.external`，通过受既有工作区认证保护的`/{task_id}/notifications`接收非空幂等键与JSON对象payload，不依赖Gateway调度或调用方轮询任务revision。接受事务冻结当时任务/定义revision、原request_id和payload；重试先查原触发，因此编辑或暂停不改变既有接受结果，同键异内容明确冲突。新通知只接受active的外部触发任务；暂停、终结或组件不匹配不得建立新运行。payload作为不可变触发事实进入schema18的原运行表，脚本从stdin读取，动作以不可信正文传给原source owner，恢复不读取当前外部服务猜测旧输入。既有manual/interval/cron内容及恢复幂等preimage保持不变，v16/v17沿有序SQL迁移保留原运行身份与接受顺序。

后端恢复时先对账在途判断与动作，再推进计划。已存在 durable execution/wakeup 的动作复用原身份，未确认脚本副作用的运行显示中断或结果未知，不盲目重放。missed schedule 沿用显式 skip/run_latest/catch_up；进程停机不承诺执行。任务事件经现有 SSE 传播，错误必须可见。

`builtin.execution_end`绑定同工作区明确Thread来源URI。Job终态事件仅唤醒统一worker；worker从原rollout的已提交terminal_convergence读取有界页，用户与内部执行共用原执行身份，每个execution只接受一次；接受前采用同一快照中已收敛的终态修正，游标之前已结束的执行不会因后续控制修正再次触发。任务只保存来源/definition revision/提交游标，原终态引用、运行及游标在Workspace schema19的同一事务提交；断电丢失通知后仍从canonical恢复，不复制执行正文或新建事件账本。分页按首次结束提交推进，精确终态引用允许指向之后的控制修正，不能按修正位置排序而跨页跳过另一执行。新建、更换来源从明确读取时的当前提交起点开始；非来源编辑保留游标。暂停后恢复从当前提交重新订阅，跳过暂停期间未接受的结束事件，已接受记录保留并遵守revision抑制。来源缺失报needs_attention，来源历史回退或损坏直接报错，不能重置游标伪装成功。模型判断thread必须与工作来源分离，防自触发在模型切片补齐。

### 6.1 启动只等待通用运行时就绪，任务恢复由受管理 worker 处理

工作区生命周期只调用通用运行时的 `start()`/`stop()`。`start()` 等待任务存储可用、事件入口完成订阅、当前 generation 的调度权建立及受管理 worker 初始化回执，然后返回；不在启动代码里 `await` 遍历所有任务、执行条件脚本、等待判断模型或等待工作 thread 完成。启动就绪表示运行时具备接收和恢复能力，不表示所有任务已经恢复或续跑成功。

同一运行时的受管理 worker 从持久索引发现待恢复任务，以有界并发逐项对账，继续统一“触发 → 判断 → 动作”链路。每个任务在恢复完成前保持可观察的 recovering 阶段，该任务的新触发先持久排队，不能越过其恢复阶段启动重复动作；恢复完成的任务可独立推进，不等待其他任务的慢判断。任务原有生命周期不因 recovering 被改成 paused 或 active。

存储、订阅或 worker 初始化失败时 `start()` 直接失败；单个任务的配置、脚本或模型失败写入该任务记录，不阻塞其他可恢复任务。worker 自身异常退出必须向生命周期 owner 报告并使任务运行时健康状态失败，拒绝声称调度成功；不能裸 `create_task` 后丢弃句柄或吞掉异常。`stop()` 关闭新调度准入并等待/收敛受管理工作与 claim，复用原 durable 身份恢复，不把停止调度等同取消已接受工作执行。

该选择替代现有启动阶段逐个 `await resume_active_goals()` 的方式，使一个慢 Goal 或模型判断不再延长整个工作区就绪等待；同时通过初始化回执、恢复阶段和失败报告避免后台启动伪装成功。既有 source/wakeup、Job 恢复 owner 继续负责各自准入，不能与任务 worker 重复分配执行身份。

### 7. Goal 与定时任务是应用，使用统一组件链路

Goal 模板限定当前 Session 的 main thread，组合目标提示、预算及结束审计、执行结束触发和内部续跑动作。现有激活、目标修改、启动恢复、Job 完成与 Agent 结束处理必须按既有行为映射，并对同一因果执行去重；不能简化为只监听 Agent 结束。模型仍通过 Goal 工具表达完成或状态变化，运行时不凭空判断目标完成。

现有定时生成器迁为 cron/interval 触发、无附加条件判断与现有三种会话动作的预设组合，采用第 2 节的统一链路，不建立“定时生成应用”专用运行时。保留时区、missed schedule、overlap、命名/导航、回报、UI 激活与失败行为。会话物理 locator 和导航以当前 catalog 合同为准，不恢复旧生成器规格中已被其他变更替代的物理 children 目录设计。

Goal 工具/API 与专用视图可以作为应用入口保留，但直接读写同一任务 owner；这不是保留旧 Goal 运行时的兼容层。生成器旧接口由全部调用方迁移后删除，不新增转发兼容接口。

迁移完成必须物理删除 `app/services/orchestration/goal_runtime_service.py` 中的 `GoalRuntimeService`，移除所有 import、容器字段/装配、独立 durable listener 注册与注销、close-admission/shutdown 关闭步骤以及 `goal_runtime_service.resume_active_goals` 启动阶段。通用生命周期、事件消费与调度进入 `PersistentTaskRuntime`；目标状态、预算、提示和完成/阻塞规则进入 Goal 应用组件。禁止保留同名服务作为转发包装层，或换个类名保留独立 Goal 恢复调度器。Goal 业务模块及工具/API 按职责保留，不因删除运行时误删业务契约。

### 8. 前端以工作区任务管理为中心，模板与高级编辑共用定义

主窗口底部面板“自动化”采用任务列表加详情。列表包含名称、模板类型、生命周期、当前阶段与原因、目标、最近结果和下次触发；详情提供配置、进度、判断/工作 thread 链接、运行记录和控制操作。

创建入口提供 Goal、定时执行、脚本条件、模型判断、自定义等快捷模板，但统一称为持久任务，不向用户暴露另一套生成器对象。纵向表单按“何时检查 → 附加条件 → 携带什么内容 → 对哪个 thread 做什么 → 何时结束”展开；选择普通定时执行时只需填写时间与动作，附加条件默认关闭并隐藏“始终通过”的内部配置。用户可开启脚本或模型附加条件，保存时修改同一任务的判断组件，不重新创建任务或切换另一套编辑器。高级配置展示条件触发模式、投递时机、补跑和并发等策略。暂不制作节点画布。

抽出可复用列表、详情、组件配置表单和运行记录；基础与高级模式编辑同一完整 DTO，不建立两套事实。组件配置类型与后端 schema 对应，未知类型/版本明确显示不可编辑原因。检查试运行会实际运行脚本或判断模型，界面说明执行位置与模型调用，并且不自动执行后续工作动作。

主窗口右侧侧边栏保留 Goal 专用标签，展示当前会话主线程 Goal；另提供当前会话关联任务摘要及跳转底部面板的入口，复用相同任务 identity 和后端对象。左侧侧边栏不新增 Gateway 任务管理。切换工作区/会话时按 owner 重置展示态，业务更改成功后使用后端完整对象替换镜像，失败重新获取并展示错误。

工作区界面就绪后，任务可能仍处于恢复中；列表与详情展示 recovering、进度及失败，不将“后端已就绪”渲染为“所有任务已恢复”。恢复中的暂停等管理操作仍使用 revision 校验，迟到恢复工作必须尊重新的任务状态。

## Risks / Trade-offs

- [迁移中重复续跑] → 切换前停止旧 producer，按稳定 ID 与触发标记导入并核验，成功后仅允许新 owner 调度。
- [判断模型成本与循环] → 判断由显式触发产生，限制同任务判断重入，隔离工作/判断事件，展示模型调用记录。
- [脚本产生外部副作用] → 明确执行位置、超时、输出和结果未知状态；不承诺 exactly-once 外部副作用，不自动重放无法确认的动作。
- [任务等待被误认为卡住] → 后端给出阶段、原因、最近判断和下次触发，界面提供立即检查与跳转。
- [暂停与已接受执行的边界] → revision 检查阻止新投递，保留已接受执行记录，取消执行独立操作。
- [相关 OpenSpec 正在实施] → 实施前重新核对上下文及 multi-workspace owner 合同，只扩展其端口，不复写其权威模型。

## Migration Plan

1. 盘点 Goal、生成器、API/工具、配置与前端的直接/间接调用方和测试，建立行为映射；先迁全声明和消费者，再集中验证。
2. 为工作区引入统一任务 owner、组件和管理 API，通用 `start()` 只等待基础设施及受管理 worker 初始化，任务异步恢复；打通无附加条件、脚本条件与模型条件的同一纵向链路。
3. Goal 的 `goal.json` 一次性导入所属工作区任务存储，保留 Goal ID、主线程定位、状态、预算、revision 和已续跑标记；原文件停止作为事实源，并删除第 7 节列出的 `GoalRuntimeService` 全部装配与启动/关闭 hook。
4. Gateway 生成器按既有 execution workspace 定位 owner，校验其受支持的 placement/context/target 关系；保留生成器 ID、运行 ID、幂等键、计划游标、未完成执行和回报引用。不可定位或离线时报告未完成迁移，禁止丢入默认工作区或改绑。默认工作区规则只用于新建时的缺省参数。
5. 导入可重试且不重复生成任务；核验成功后在单次切换中停止并移除旧调度、存储、接口及配置消费者，旧文件只作为显式迁移/备份输入，不供生产双读。
6. 回退仅通过停止调度后恢复迁移前备份及对应源码版本；新旧运行时不能同时调度同一任务，已发生的外部执行不能靠回退存储撤销。
7. 完成任务规格对应的定向行为、真实类型检查、Web 构建、恢复窗口、迁移和真实浏览器验证。8111 为当前开发版目标，端口使用实际启动配置，不能硬编码进入产品。

## 实施盘点与切片顺序（2026-10-27）

### 调用方与旧事实源

以下是当前源码的迁移闭包，不能仅替换三个旧服务的类名。全仓检索命令、命中文件及SHA-256保存于 `out/tests/temp/persistent-task-migration/artifacts/inventory-002/inventory.json`；生成的Python/TypeScript类型从两份权威proto重新生成，两份OpenAPI随公开入口一起更新。

| 业务链路 | 当前权威入口及消费者 | 迁移时必须一并处理 |
|---|---|---|
| Goal状态与预算 | `session_goal_service.py`、`session_goal_store.py`、`internal_v2/goal.py` | Session节点`goal.json`是旧权威；main指针只来自catalog，不把响应中的thread_id再持久化为可变归属 |
| Goal调度 | `goal_runtime_service.py`、`container.py`、`workspace_lifecycle.py` | 五类Job/Agent事件、durable listener、启动恢复、停止准入与后台排空全部移入通用运行时；不能仅改启动hook |
| Goal工具与HTTP | `agents/tools/goal.py`、`agent_tools.py`、`agent_factory.py`、`runtime/agent_runtime.py`、`api/sessions.py`、`api/deps.py` | 保留Goal业务入口，删除对旧runtime的依赖；创建/修改/清除前的进度结算和激活行为一起迁移 |
| Goal创建与fork | `core/session_creation.py`、`session_context_fork_service.py`、`SessionGoalService.install_fork_goal` | 现有创建journal会在preparing节点安装并校验goal.json；改用统一任务owner后，原创建能力、发布可见性、取消与重启恢复也须迁齐，不能留下要求旧文件的物化检查 |
| Goal上下文与UI | `prompting/registry.py`、`job_execution_service.py`、checkpoint metadata；`sessionGoals.ts`、`useSessionGoalController`、`state/sessionGoal`、`GoalManagerCard`及其上层会话控制器 | 专用表单直接消费同一任务；保留内部提示的真实来源与预算状态，不把任务通知变成用户输入 |
| Gateway生成器定义 | `gateway/control/generators.py`、`control/router.py`、`schemas/gateway_control.py` | 由`get_gateway_root()`解析的`generators`、`generator-schedules`、`generation-runs`均是待导入事实；保留显式根目录覆盖，不硬编码全局路径；只删除生成器路由，保留同文件中无关的Gateway导航与资源管理 |
| Gateway生成器调度与观察 | `control/scheduler.py`、`control/coordinator.py`、`gateway/lifespan.py` | 定时循环、恢复dispatching、远端Job/回报监控及关闭任务一并移除；不是只将cron移动到后端而保留Gateway终态writer |
| Gateway配置与健康 | `gateway/config/values.py`、`config/loader.py`、`runtime_proof.py`、`routes/health_config.py`、Gateway JSONC/schema与config policy | 移除旧生成器poll参数及其prepare/apply/rollback/health proof，避免配置应用继续访问已删除调度器；其余Gateway运行时参数保留 |
| 工作区会话动作 | `business/session_generation/{service,providers,message_dispatch,reporting}.py`、`api/session_navigation.py`、`internal_v2/session_navigation/models.py` | 当前还保存工作区`.boxteam/generation-runs`及Session内`generation_intents`，并在start扫描恢复/启动终态观察；它们也要并入任务运行，不再与Gateway运行记录各自维护可变终态 |
| 生成器前端 | `gatewayApi.ts`、`useSessionGeneratorResources`、`useSessionResourceExplorer`、`SessionGeneratorManager`、`sessionGeneratorPresentation`及底部面板装配 | 将Gateway API与轮询移到工作区入口；前端不能仅按工作区筛选Gateway全局列表来声称完成迁移 |
| 协议与验证 | `proto/boxteam/{workspace/v2,gateway/v1}/public.proto`、对应生成资产、两份OpenAPI；Goal unit/locks/tools/runtime、Gateway control scheduler/coordinator/auth、generation strategies/recovery、挂载生命周期和Web组件测试 | 原有身份、预算、调度点与恢复断言随调用方迁移；模型响应继续复用model_stream，不能手工填入任务或执行状态替产品完成步骤 |

检索识别出的自动producer是Goal事件/启动续跑、Gateway时间调度，以及工作区SessionGeneration的在途动作/回报恢复；后者虽然不是独立cron，也属于必须迁移的恢复与终态writer。手动run及API激活同样进入统一触发；不以删除定时器代替完整迁移。

### 复用端口与存储边界

- 任务定义、触发和运行采用现有 `WorkspaceStateStore` 的同一工作区SQLite（`.boxteam/state/workspace.sqlite`）及SQL资产加载/迁移原语，新增唯一任务方法族；不在Gateway或Session另建任务数据库。只有实际增加表/索引时才提升当前schema版本。
- 上下文权威继续是Saver/ContextStore的 `ApplySourceLifecycleDecision`；`observe_pending`只表示已保存、未applied。当前 `SessionOrchestrator.submit_runtime_event → RuntimeSourceDispatcher.submit → commit_runtime_source_wakeup` 已原子保存source、wakeup与队列输入，继续用于context_and_run；context_only尚缺业务端口，不能调用该方法再丢弃返回值来假装没有启动执行。
- 后续context_only端口与context_and_run共用相同source构造和幂等校验，均经过Session生命周期准入与精确Thread校验；任务owner只保存回执与引用，不复制正文或applied状态。可更新状态的revision由原source合同表达，不把每次轮询都当作一次性notice。
- 目标执行继续复用RuntimeSourceDispatcher及thread rollout的单事务队列，判断thread也走现有执行准入。持久任务不是Job、BackgroundTask或外部资源lease的替代模型；MCP授权观察等现有BackgroundTask handler不因本变更另存一套任务/邮箱事实。
- 工作区挂载生命周期只装配本工作区的运行时。Session定位、main_thread_id和物理目录仍取catalog；Terminal/Browser由ExternalResourceLeaseLedger及其原owner管理，任务只持有必要引用。Goal fork的新任务要沿原Session创建journal幂等安装，尚未发布的Session不得被恢复worker提前调度。

### 必须保留的行为映射

| 现行行为 | 统一任务中的约束与对应验收 |
|---|---|
| Goal六种状态与预算 | active/paused/blocked/usage_limited/budget_limited/complete仍是Goal应用状态；只有active的预算耗尽自动成为budget_limited，不能覆盖用户选定的complete/paused。对应Goal应用及暂停场景 |
| Goal记账与续跑 | 以job_id记token/elapsed的单调增量，保留time_closed、accounted_jobs和last_continued_job_id。JOB_STARTED冻结Goal归属，AGENT_END记账/续跑，JOB_COMPLETED补偿空闲激活，FAILED变blocked、CANCELLED变paused；多个事件不得重复续跑。对应多事件同一执行结束场景 |
| 激活、编辑与清除 | 创建/重新active在main空闲时续跑；目标更新按原revision身份排队，外部暂停/清除先结算在途用时；暂停不取消已接受Job。对应revision、生命周期及Goal视图场景 |
| 启动与fork | active Goal由统一worker恢复，无单独resume_active_goals。fork显式新建Goal时保留目标和指定预算、使用新Goal ID/目标创建时刻，用量及逐Job标记从零开始；child不获得Goal。对应慢恢复、重启及独立fork Goal场景 |
| 定时与并发 | 保留manual/cron/interval、ZoneInfo时区和skip/run_latest/catch_up；计划点幂等键不变，overlap=allow与continue_existing的thread queue分属不同约束，不能将判断串行化变成所有工作Job互斥。对应有效计划点、补跑及忙时队列场景 |
| 三种动作与上下文 | new_per_run/continue_existing/fork_new_and_report_back复用原会话动作；保留fresh/live_session/snapshot验证及明确Thread，现行placement、execution、target/context的同工作区限制不扩大。对应三种策略及稳定定位器场景 |
| 回报、命名与UI | none/link/summary/summary_and_link/full/continue_agent、稳定输出locator、预留execution identity、原消息intent与回报Job全部随运行导入；标题/导航模板不决定物理目录，on_run_started/on_run_completed仍只影响界面。对应回报、预览、重启幂等及真实Web场景 |

迁移按原execution_workspace_id定位目标，禁止把Gateway注册表路由ID直接当作后端owner UUID；关联定义、计划游标、Gateway运行和工作区运行/消息intent一并校验。先关闭旧producer准入并排空/记录在途，再按稳定ID导入并逐项核验；离线或身份冲突保留明确未完成状态，目标任务不可调度，也不重启旧producer并行工作。核验通过后启用同一运行时，旧文件仅作显式迁移/备份输入，最终删除上述旧writer和消费者。

先打通手动/时间触发、显式always判断、单一任务运行与source动作，再迁Goal，最后迁定时会话动作和Web入口。团队协作设计继续TODO，不作为该独立链路前置；真实Provider缓存指标和已测量的性能长尾也不阻塞任务正确性实现。以上为源码/合同盘点，尚不代表新任务模型、存储、运行时或产品验收已完成。

### 仅发布事件端口的实施与证据（2026-10-27）

`SessionOrchestrator.publish_runtime_context` 经既有 `RuntimeSourceDispatcher` 和 source mutation owner 保存不可变事件；ambient canonical item 与 active view membership 在同一 rollout 事务提交，不分配 Job、Execution、Turn 或 wakeup。回执只表示已保存。相同幂等键复用同一 item，异内容及切换为执行投递均明确冲突；幂等重试不把已从当前 view 移除的历史 item 再次选入。

短写继续使用既有 context_control lease，以具名版本化 recovery_ref 定位精确 Thread/item，不保存第二份正文。启动及 Session 删除 drain 对照原 canonical 记录校验 source revision 与准入 preimage：已提交者完成结算，无提交者明确失败，损坏或冲突直接报错；不重放正文、不创建恢复执行。恢复后原事件身份保持不变。

- `context-only-product-020`：正式 matrix 套件 5 passed，111.23 秒；真实 Session 创建及消息入口证明空闲发布无执行、忙时不改已发送请求且下一次工具循环可见。公开 fork 入口配齐真实 Terminal/Browser 服务和原 60 秒 watchdog，证明只复制锚点前来源，冷读保留来源及投影身份。故障注入截断 source 提交前和提交后/lease 结算前，使用全新 Saver/dispatcher 调用实际启动恢复，并经后续真实模型请求核对结果；这是冷 owner 恢复验证，尚未代替完整持久任务的进程重启验收。
- `context-only-source-regressions-018`：68 passed；另一个 fork 用例因直接 pytest 未经过正式 matrix runner 在 fixture 阶段被拒绝。`context-only-notice-fork-022` 使用原 matrix 入口补跑该项，1 passed；未重复已通过部分。
- `context-only-runtime-regressions-021`：旧 runtime notice 文本和冻结执行准入两项真实产品回归通过。`context-only-types-017` 与 `context-only-ruff-019` 均 exit 0；上述最终运行均无源码漂移。

各运行的完整命令、源码指纹、原始输出和实际退出码保存在 `out/tests/temp/persistent-task-migration/artifacts/<运行名>/`。本子项尚未实现同一来源状态更新、任务动作的投递时机协调或统一任务运行时，3.5 及相关整体验收保持未完成。


### 手动任务运行时的已实现边界（2026-10-27）

任务账本复用WorkspaceStateStore，schema 16只增加真实使用的任务、不可变定义和运行表。运行引用冻结定义，source正文及wakeup继续归原owner；管理API和SSE只读取同一工作区账本。当前支持manual/always/literal/runtime_source v1，context_only与context_and_run共享一条判断及恢复链路，不接受尚未实现的组件类型或版本。

worker的start等待初始化future，不等待逐项恢复；每个任务按序处理并有全局并发界限。恢复和新检查共用运行账本，同任务未对账的新触发只排队；任务管理与新source准入串行，已接受动作先按旧定义及原run身份查询回执，再记录结果，即便期间暂停或编辑也不会丢失既有事实。保存原Gateway authority与request_id供后台调用，既有执行owner负责模型、队列和Job，任务不等待模型终态、不取消已接受Job。worker初始化及运行失败由生命周期/健康入口明确报告，stop取回异常并排空已开始动作。

产品与协议证据见同变更tasks.md的“手动触发至source动作切片”；这里只登记已实现边界，尚未声称时间计划、脚本/模型条件、状态来源更新、Goal/生成器迁移和任务UI已完成。


### 时间触发与单一接受顺序（2026-10-27）

interval/cron调用共享TimeSchedule，旧Gateway生成器同步移除重复算法。工作区计划只保存冻结锚点、下一计划点及定义revision；SQL 17在同一事务追加触发运行并推进游标，失败两者一并回滚。非时间配置修改保留游标，修改触发器才建立新锚点；暂停保留原游标，恢复使用显式补跑策略。批量catch_up沿持久run_sequence接受顺序执行，不能依赖相同时间戳下随机ID的字典序。

通用worker按最近计划点等待唤醒，时间到期只建立普通任务运行，随后仍经always判断与原source动作。它可在某任务慢恢复期间持久排入新计划，但同任务在途恢复及动作串行；控制修改后尚未接受的动作按原revision规则抑制。自动计划为新触发创建一个请求身份，保存后恢复继续使用原身份。DTO显示明确trigger_type、scheduled_for及next_run_at，skipped计划有独立记录，不伪装成已投递。

v16历史按原可观察顺序迁入新接受序号，原run_id、定义、request_id、阶段与正文引用不改写；缺省时间字段不改变既有手动创建请求的幂等preimage。产品与迁移证据见tasks.md；这一步尚未切换Goal或生成器owner，二者仍须按已登记调用闭包整体迁移后物理删除旧owner。


### Goal调度迁移的当前边界（2026-10-27）

GoalRuntimeService已物理删除，其事件、激活及恢复工作由PersistentTaskRuntime唯一worker排队并共享并发槽位；Goal应用函数只处理单个工作项，没有独立恢复循环或生命周期。工作区启动仅等待共享worker初始化回执，关闭先停止动作准入，再排空Job终态和worker。事件同步冻结Goal归属、请求身份及观察时间；终态携带Job快照，避免注册表删除后的延迟读取。后续的存储切片已将Goal迁入同一任务owner，见下一节；瞬态工作队列不作为持久事实源。


### Goal应用的唯一持久状态（2026-10-27）

Workspace schema21扩展原任务表，以application_type=goal、owner_session_id及application_json保存Goal身份、状态/用量/续跑标记；目标和预算在原不可变定义表版本化，用量更新不复制定义。Goal六状态是权威应用状态，通用lifecycle列仅为同事务派生索引；main Thread来自catalog，不持久第二指针。清除/替换退休原任务并保留原导入身份，既有Goal API返回合同不变。通用列表和详情通过application_type/application_version投影原Goal状态，configuration只属于automation应用；Goal修改和执行继续消费原业务入口，通用编辑/控制及关联UI仍由2.1/6.5实施，不能把Goal定义当普通自动化配置读取。

启动在恢复fork及启动共享worker之前显式一次性导入旧goal.json；原始字节指纹与目标写入同一事务，核验成功后删除并fsync。重试只核对原导入回执，不能覆盖后续用户修改或复活已退休的Goal。fork继续按原creation journal安装、发布和撤销，准备阶段不可被调度；创建目录不再允许或要求goal.json。容器显式复用唯一PersistentTaskStore，旧SessionGoalStore已物理删除。完整进程窗口组合验收继续由7.4追踪，当前定向证据见tasks.md。


### 应用查询与恢复的当前边界（2026-10-27）

公开PersistentTaskDTO将共同的任务身份/版本/生命周期与应用状态分开：automation携带原组件配置，goal携带同一Goal DTO，互斥且核验Goal ID。内部AutomationTaskDTO只收窄组件执行需要的不可空配置，不引入第二套序列化或持久化。管理列表/详情读取全部已发布应用，普通组件worker只读取automation；所有数据仍由同一WorkspaceStateStore保存。Goal更新/清除沿原事件触发完整快照失效，任务通知不保存另一份Goal镜像。

Goal 续跑在原 Goal 锁内先提交 source/wakeup 准入，接受成功后再登记 last_continued_job_id；失败不写尝试标记，也不需要补偿回写。接受前进程中断由统一 worker 恢复原 Goal 并重试原幂等链路。此处已验证接受前硬重启；接受后标记前中断及完整在途执行对账仍需独立产品验收，不能据此视为所有恢复窗口完成。

启动枚举Goal及普通任务恢复身份，在初始化回执后才推进单项工作。Goal恢复工作冻结原目标身份和revision，管理查询展示recovering；暂停等操作仍更改原Goal状态，迟到恢复不能改回active。成功结束恢复后清除瞬态进度并通知同一管理列表。通用组件管理暂时明确拒绝Goal动作，等待6.5将表单接到既有Goal规则；不得直接修改同事务派生的lifecycle索引。此查询切片不代表会话生成器或主Web管理已完成，产品/合同证据见tasks.md。
