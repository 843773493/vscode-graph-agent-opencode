## Why

当前自动压缩在模型请求前同步生成摘要，用户会在上下文接近上限时等待；阈值又主要依据 Agent 初始模型，无法充分处理 fallback、小窗口模型、fork 和新来源注入后的实际容量。摘要生成与应用耦合，且溢出重试可能丢弃待摘要范围的前段，不能作为可复用的后台准备机制。

## What Changes

共同派生执行依赖 `unify-derived-execution`／`derived-execution` capability；本change保留压缩业务，不另实施一套公共准入、工具循环、调度或发送器。

- 统一上下文容量规划与准备入口：按既有顺序为真实 Provider／Model 计算冻结输入的预算与能力，实际采用时才改变view，复用现有 canonical、CSM、ToolSet、Saver 和 seal owner。
- 默认达到提前阈值时冻结可安全替换的历史范围，后台生成摘要候选；达到应用阈值时先模拟可用候选的完整请求，足够则快速采用；不足时先走同链阻断生成完整提案，每次准备至多一次提交压缩。支持显式提前应用及包含新增内容的后续合并摘要。
- 同步生成、合并及注入提示词：摘要只反映冻结历史范围末尾的事实，后续保留消息可更新其状态；不声称掌握最新会话，不指示模型无条件执行旧计划。
- 默认按有效输入预算80%准备、90%应用，目标55%；不引入固定65k安全阈值。90%处因已运行的后台摘要尚未完成而真实等待并成功后，按任务去重将未来准备线每次提前5个百分点，默认最低60%；仅运行时按Thread／模型学习，不写回用户配置。
- 摘要候选与 active view 分离，按真实 Item、消息组、工具协议闭合和来源版本证明适用性；尾部追加可保留候选，rewind、编辑、来源变化不能误用候选。
- 按实际候选模型重新检查容量，包括大窗口到小窗口 fallback；保留不可压缩来源、新输入和必要工具关系，不伪造输出上限、不自动丢弃内容。
- 统一普通请求、手动压缩、fork、消息／附件注入、上下文来源变化和中断续跑的准备合同；不另造每种业务的压缩器、后台 Agent Turn 或外部队列。
- **BREAKING**：移除“截掉前半段后把摘要当作覆盖整个范围”的摘要溢出链；采用有界、全覆盖的分块与合并，不能以截断冒充成功压缩。
- 限制每 Thread 在途任务、候选数量、工作区后台并发和重试；沿现有任务、日志、实时通道报告状态，保证用户中断与前台业务优先。自动压缩关闭时不自动生成或采用候选，策略阈值不充当真实硬上限。
- 复用现有BackgroundTaskRegistry／SessionControl任务身份、租约、驻留与取消，rollout只拥有候选结果和采用事实；派生请求不新增任务管理器或普通Agent执行槽。
- 接入 `unify-derived-execution` 的共同请求链与缓存前缀合同，摘要禁止实际工具执行但不因而清空来源tools或替换root；后台不伪造Graph runtime、不结算当前未完成工具、不刷新主CSM或污染主请求上下文／usage。
- **BREAKING**：以Saver active selection统一当前运行、checkpoint恢复、上下文查询和replay，删除消息offset事件二次投影；压缩原文回读复用canonical查询，停止另写／重写history.md，保留现有回读工具入口。
- 明确手动压缩的Thread目标、模型工具与HTTP请求的统一intent，以及后台准备和实际采用的不同UI状态。

## Capabilities

### New Capabilities

- `context-budget-preparation`：实际模型容量、完整请求预算、统一准备结果、fallback 与不同上下文业务的接入边界。
- `background-context-compaction`：提前触发、冻结范围、候选生命周期、后台生成、完整覆盖、原子应用与有界调度。

### Modified Capabilities

- `context-injection-lifecycle`：明确 Provider 重投影与实际 compaction 是不同变更原因；小窗口 fallback 可以先显式压缩再封存新请求，后台候选不得修改正在执行或已封存的 assembly。
- `checkpoint-context-branching`：以当前语义 Item 和协议闭合约束压缩边界；fork、rewind 与候选应用共享原 owner，候选不能成为第二份分支历史。

## Impact

主要影响 `app/agents/summarization/`、`model_capability_routing.py`、`model_call_execution.py`、原BackgroundTaskRegistry／SessionControl、手动API／模型工具、MessageService／replay，以及 rollout checkpoint／view／assembly owner。候选结果与任务执行分别沿各自原存储owner扩展，以幂等引用衔接；配置沿 Workspace 配置域及现有 Provider／Model 目录解析，不增加 Gateway 业务状态、全局摘要数据库或独立守护进程。状态展示沿既有上下文查询、实时通道和前端上下文通知，不新建专用控制面。

承接 `normalize-semantic-items` 的当前格式3与独立语义 Item；不恢复 Part、物理 message offset 或旧格式 reader。该前置变更先归档，本变更随后归档。配置继承和错误展示分别复用并发变更 `unify-configuration-inheritance`、`unify-failure-reporting` 的最终合同，不复制加载器或错误协议。实施时核对实际结构版本与发布证据，不预占 SQLite 版本号，不据开发残留新增自动转换。
