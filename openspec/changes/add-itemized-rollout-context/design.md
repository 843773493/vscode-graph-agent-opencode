## Context

当前 `app/core/rollout_storage.py` 以完整 LangChain message 作为 `rollout.jsonl` 的不可变记录，并在 SQLite 中维护 `messages`、tool call 和 reasoning 的投影。当前消息流协议已经有 block 生命周期和增量事件，但它是实时展示/恢复通道，不等同于 rollout 的 canonical history。现有 middleware 还会直接对 `ModelRequest` 的 messages、system message 和 tools 做请求级修改，缺少统一的来源、版本和 assembly provenance。

参考仓库的 `canonical-response-protocol` 提供了 OpenAI Responses 风格的 `OutputItem`、item 生命周期和 source identity；`itemized-context-runtime` 又明确区分了 canonical item、view index、semantic request plan 和 provider wire message。这里吸收这两个边界，但不直接把参考仓库的 `OutputItem` 或 prototype runtime 搬进生产代码：前者是 provider response 协议，后者明确是独立 prototype。

设计必须继续满足当前项目的历史、compaction、rewind/replay、fork、LangGraph checkpoint 和实时 SSE 约束：JSONL/SQLite 的恢复边界保持可验证，前端仍消费后端 projection，provider raw chunk 不成为公共持久化模型。运行时上下文要区分持久化历史事实与本次请求临时贡献：静态 system prompt、动态 skill/环境/记忆提示和 tool definition 默认只属于 request-only context，但其来源、版本和 hash 仍可进入 assembly metadata。实时内存 ledger 要能为当前 Turn 记录来源和 metadata，同时将可恢复的稳定引用落到 SQLite；未来扩展可以按引用读取详情，但本 change 不实现前端。

## Goals / Non-Goals

**Goals:**

- 建立一个稳定、版本化、provider-neutral 的 canonical item 事实层。
- 让 item 的物理顺序、语义身份、Turn/group 关联、tool 因果关系和 reasoning 保护状态可恢复、可审计。
- 从 active context view 生成多种执行投影：LangChain `BaseMessage[]`、Provider 原生 item/request，以及历史/前端的轻量 projection。
- 让实时 block delta、item draft、终态 JSONL item 和 checkpoint commit 具有清楚的职责边界。
- 为旧版 message-line rollout 提供显式版本识别，以及只在一次性 `legacy_import_v1_to_v2` migration/import 命令中使用的读取、报告、quarantine 和 rollback audit 路径；正常 runtime 不提供 v1 只读读取。
- 让 middleware 的上下文贡献、tool set、item producer 和一次 model call 的 assembly 具有可追踪 provenance，并为未来详情视图保留稳定引用。
- 按恢复、历史投影和操作需求分层 SQLite item 索引，使不需要详情或操作的 item 不承担不必要的索引成本。

**Non-Goals:**

- 不把 OpenAI `OutputItem` 直接作为全局领域类型，也不要求所有 provider 使用同一个 wire schema。
- 不把 LangChain 改造成保存 `list[CanonicalItem]` 的存储系统；LangChain 仍只接收一次执行所需的 `list[BaseMessage]` 投影。
- 不把每个 raw chunk 或每个 `block.delta` 写成一个永久历史 item。
- 不在本变更中重做 SSE、前端 reducer、Agent 调度、工具执行器或 provider SDK。
- 不在本变更中实现 provenance 的前端展示；只提供后端/存储可扩展的 identity、reference 和受保护详情边界。
- 不删除作为 migration/rollback audit 的 v1 原始 artifact 或既有已完成 OpenSpec 变更；不保留旧 session/history/provider/checkpoint runtime API 作为兼容路径。旧聚合职责和临时 import shim 的删除必须等待实现验证与主 spec 同步，并作为本 change 完成门槛。

## Decisions

### 1. 采用分层上下文边界，而不是让一个类型贯穿全链路

生产链路固定为：

```text
CanonicalItemStore + ActiveContextView
        + RuntimeContextSources / Middleware
        + ToolPolicy / ToolRegistry
                         ↓
              ContextRequestPlan
          (CanonicalItemRef | RequestItemRef)
          ┌──────────────┼────────────────┐
          │              │                │
  LangChainProjector  ProviderProjector  HistoryProjector
  list[BaseMessage]   native request     Turn/detail API

LiteLLM/provider raw chunk
        ↓
NormalizedModelDelta → MessageStreamRuntime / ItemDraft accumulator
                                      ↓ finalization
                     CanonicalItemWriter → rollout.jsonl + SQLite catalog
```

`NormalizedModelDelta` 是 provider 到业务层的输入边界；`CanonicalItemRecord` 是持久化事实；`ContextContribution` 是 middleware/runtime 对请求的结构化贡献；图中的 `CanonicalItemRef | RequestItemRef` 只是内部语义角色，序列化 plan、snapshot、SQLite assembly ref 和 projector 输入统一使用 `ContextRef{ref_type, ref_id}`；`ContextRequestPlan` 是一次请求的选择结果；`BaseMessage[]` 和 Provider request items 都是目标投影。`wire_role` 只表示目标 Provider 的编码角色，不能作为 canonical 身份或来源分类。这样可以避免 LangChain 的 message grouping 规则反过来决定历史事实，也避免历史 reader 依赖某个 provider 的 wire shape。

### 1.1 生产模块归属与可执行拆分顺序

历史上的 `app/core/itemized_rollout.py`、`app/core/itemized_context_runtime.py`、`app/core/itemized_projection.py`、`app/core/rollout_storage.py` 和 `app/core/rollout_checkpoint_saver.py` 是迁移期聚合实现，不是最终模块边界。它们混合了领域 schema、会话持久化、middleware runtime、LangChain/provider 映射和 checkpoint/fork 流程；本 change 不把旧文件已被拆出若干模块视为 3.2 或任一架构拆分任务的完成证据。`app/core/AGENTS.md` 规定 core 只承载通用内核，因此拆分后不得继续向这些聚合文件添加会话、消息、Agent、工具或 provider 流程。v2 domain、storage、runtime 和 projection 是唯一生产运行时事实源；旧聚合路径在提取期间至多提供有明确删除门槛的临时 import shim，不能作为正常运行时 fallback、兼容 API 或第二事实源。

最终归属和依赖方向冻结如下：

| 当前聚合文件 | 目标模块/目录 | 目标职责 | 明确不再归属 |
|---|---|---|---|
| `app/core/itemized_rollout.py` | `app/domain/itemized/` 下的 `schema.py`、`enums.py`、`refs.py`、`selection.py`、`request_plan.py`、`assembly_snapshot.py`、`serialization.py`、`validation.py`、`hashing.py`；legacy reader/report/quarantine 另归 `app/services/infrastructure/rollout_context/migration/` | 纯 `CanonicalItemRecord`、枚举、ContextRef/ToolSetRef、plan/selection、assembly snapshot、兼容矩阵、JCS preimage/hash；原 `plans.py` 只能作为拆分来源，完成后不得保留重复领域职责；旧格式只由一次性 migration 工具读取 | SQLite、路径 I/O、LangChain、Provider、Agent 流程，以及被正常 runtime 导入的 v1 adapter |
| `app/core/itemized_context_runtime.py` | `app/services/infrastructure/rollout_context/runtime/` 下的 `detail_store.py`、`ledger.py`、`composer.py`、`reconciliation.py`、`stream_accumulator.py` | detail store 安全边界、实时 ledger/composer、source reconciliation、stream draft accumulator | canonical domain schema、provider wire 编码、通用 `app/core` 内核 |
| `app/core/itemized_projection.py` | `app/services/mapping/itemized/` 下的 `history.py`、`langchain.py`、`selection.py`；Provider-specific bridge 进入 `app/services/infrastructure/rollout_context/provider/` | 从 Saver 返回的已提交 plan/selection 做无存储旁路的历史/LangChain/provider 映射 | JSONL/SQLite 读取、业务决策、独立重新排序 |
| `app/core/rollout_storage.py`（及当前 `app/services/infrastructure/rollout_context/storage/service.py`） | `app/services/infrastructure/rollout_context/storage/` 只保留薄 facade 与 `jsonl.py`、`catalog.py`、`commits.py`、`recovery.py`、`schema.py`；checkpoint/view/anchor 归 `checkpoint/`，turn/execution/model-call 归明确的 execution 子包，context assembly 归 `assembly/`，fork/compaction/pruning 归 operation 子包；history/projection/mapping 归 `app/services/mapping/itemized/` | facade 只做依赖组装、port 路由和事务边界；各聚焦模块分别负责 v2 JSONL、catalog/index、commit/recovery/schema、checkpoint/view/anchor、Turn/execution/model-call、assembly manifest/detail/overlay、fork/compaction/pruning | storage 不得实现 LangChain/provider wire、纯 mapping、history projection、业务规则、Agent 编排或一次性 v1 reader；不得直接 import `langchain_core.messages` 或 `app.services.mapping.itemized` |
| `app/core/rollout_checkpoint_saver.py`（及当前 `checkpoint/saver.py`、`checkpoint/owner.py`） | `app/services/infrastructure/rollout_context/checkpoint/` 下的单一 `context_owner.py` facade、`persistence.py`、`view_anchor.py`、`fork_compaction.py`；assembly manifest/detail/overlay 由 `assembly/` owner 提供 | Saver context owner、checkpoint/view/anchor 持久化、fork/compaction adapter，各自只通过 domain/storage port 协作 | 不得让 `saver.py`、`owner.py` 各自保留一套 owner/事实源；不直接实现 LangChain/provider mapping、middleware 业务规则、JSONL 细节或 v1 runtime fallback |

#### 1.1.1 已提取大文件的二次拆分硬门槛

当前 v2 提取结果中的大文件仍不是最终架构边界，必须在 7.5-F 前完成下表拆分，或提交可复查的架构审查证据证明目标职责已经分别由单一 owner 承担；仅增加 helper、局部静态检查或保留长期 facade 不算完成：

| 当前大文件 | 必须下沉的聚焦模块 | 依赖与删除门槛 |
|---|---|---|
| `app/services/infrastructure/rollout_context/storage/service.py` | `storage/jsonl.py`、`catalog.py`、`commits.py`、`recovery.py`、`schema.py`；`checkpoint/view_anchor.py`；`execution/turns.py`、`execution/executions.py`、`execution/model_calls.py`；`assembly/manifest.py`、`assembly/detail.py`、`assembly/overlay.py`；`operations/fork.py`、`operations/compaction.py`、`operations/pruning.py`；`app/services/mapping/itemized/` 中的 history/projection | `service.py` 最终只能是薄 facade，不能 import LangChain 或 mapping；storage 只依赖 domain、session path resolver 和 I/O port；mapping 只接收 Saver/domain DTO 且无 I/O。实际行数、直接文件数及已拆分/仍未完成状态只记在 tasks Verification ledger；原聚合职责和临时 shim 必须在 v2-only import/runtime 审计后删除 |
| 原 `app/domain/itemized/plans.py`（仅历史拆分来源，当前不保留） | `refs.py`、`selection.py`、`request_plan.py`、`assembly_snapshot.py`、`serialization.py`，共享校验/hash 只由 `validation.py`/`hashing.py` 持有 | 只能依赖标准库和 JCS 实现；不得依赖 storage、路径、LangChain、Provider 或 orchestration；不得恢复 `plans.py` 长期第二领域入口 |
| `app/services/infrastructure/rollout_context/assembly/store.py` | `assembly/manifest.py`、`assembly/detail.py`、`assembly/overlay.py`、`assembly/selection.py`；detail security 与 overlay reconciliation 分属明确 owner | 由 domain selection/assembly contract 驱动，读写通过 storage/detail port；不得复制 canonical item、贡献 ledger 或 projector；每个 sealed manifest 只有一个 assembly owner。实际拆分状态只记在 tasks Verification ledger |
| `app/services/infrastructure/rollout_context/checkpoint/saver.py` 与 `checkpoint/owner.py` | `checkpoint/context_owner.py`（唯一 plan/assembly owner facade）、`checkpoint/persistence.py`（checkpoint/view/anchor durable commit）、`checkpoint/fork_compaction.py`（fork/rewind/compaction adapter）；assembly manifest/detail/overlay 继续由 `assembly/` owner 负责 | `saver.py` 与 `owner.py` 不得并行保留 owner 事实；context owner 只能向业务提供 Saver port，persistence 只负责提交/恢复，fork_compaction 只负责操作适配。实际是否仍为聚合入口及调用方迁移状态只记在 tasks Verification ledger；完成调用方迁移后删除旧聚合职责和 shim |

上述拆分的依赖方向固定为 `domain -> ports`、`storage/assembly/checkpoint -> domain + I/O`、`mapping -> domain/Saver DTO（无 I/O）`、`provider wrapper -> normalization/唯一 ToolSet bridge`、`orchestration -> ports/events`；storage 不得反向依赖 mapping/LangChain，mapping 不得依赖 storage/provider SDK，orchestration 不得保存 canonical/storage/provider 事实。每个新建 source subdirectory 必须在同一变更中创建包含“目录用途”“可修改内容”“不可修改内容”“规范”四部分的 `AGENTS.md`；已有目录沿用现有 `AGENTS.md`，不得重复创建。任何新增或重构源码文件超过 800 行、混合两个以上领域职责，或目标目录直接源码文件超过 20 个，均必须在 7.5-F 前拆分或提供包含文件清单、职责/owner 映射、import graph、行数统计和复核结论的架构审查证据；不得用长期 compatibility shim、dual writer、dual projector、双 schema 或双事实源规避该门槛。

同一拆分计划还覆盖本 change 已触及的四个大型应用层文件。它们不能各自保留一套 itemized 事实或 ToolSetRef 投影：

| 当前聚合文件 | 目标模块/目录 | 保留职责 | 必须移出的职责与唯一 owner |
|---|---|---|---|
| `app/agents/providers/litellm_content.py`（约 574 行） | `app/agents/providers/` 内的 `output_normalization.py`、`stream_normalization.py`；纯 history/LangChain 映射移至 `app/services/mapping/itemized/provider_history.py`；ToolSetRef 外部 wire bridge 统一由 `app/services/infrastructure/rollout_context/provider/toolset_request_bridge.py` 持有 | LiteLLM/Responses/Chat Completions wrapper 边界、provider raw response 到项目既有 LangChain 有序 content block 的格式归一化、provider-level stream chunk normalization | canonical item/selection 事实、无 I/O history/LangChain 映射和 ToolSetRef→provider wire schema 不得留在该聚合文件；wrapper 只能调用唯一 bridge，不得在 `app/agents/providers/` 再实现一份 ToolSetRef 编码 |
| `app/services/business/message_service.py`（约 1020 行） | `app/services/mapping/itemized/reasoning.py`、`message_dto.py`、`attachment.py`、`agent_state.py`、`history.py`；cursor/page/service entrypoint 归入已有 `app/services/business/session_turn_history/` | session/message 业务规则、权限/可见性、cursor/page 语义和业务 service entrypoint；通过 Saver port 取得已提交 view/plan | reasoning merge、message/attachment DTO 转换、agent-state/history 纯投影移出 business；business 不拥有 item payload、JSONL/SQLite reader、canonical hash 或 ToolSetRef wire 编码 |
| `app/services/orchestration/agent_execution_service.py`（约 1555 行） | `app/services/orchestration/step_control_loop.py`、`retry_reminder_policy.py`、`checkpoint_context_adapter.py`、`tool_event_transition.py` | step/control loop 的顺序编排、重试/reminder policy 的调用时机、checkpoint/context port 适配、tool execution/event transition 的流程协调 | checkpoint/context 的持久化由 infrastructure/checkpoint owner 负责，canonical item/storage/provider response 事实由对应 domain/provider/storage owner 负责；orchestration 只能传递协议和事件，不得保存或重建这些事实 |
| `app/services/orchestration/message_stream_runtime.py`（约 905 行） | `app/services/orchestration/trace_observer.py`、`stream_block_assembler.py`、`tool_call_registry.py`、`terminal_finalization.py` | 实时 trace 观察、已归一化 block/delta 的时序组装、临时 tool-call registry、实时 terminal/finalization 状态机 | provider raw 输出归一化由 `app/agents/providers/` owner 负责；durable canonical item/terminal outcome 由 Saver/storage owner 负责；stream registry 只保存实时引用，不成为 canonical item 或 provider content 的第二事实源 |

四个应用层边界的接口合同进一步冻结如下：

1. **provider response normalization** 唯一归属 `app/agents/providers/`：它把 LiteLLM/raw response/chunk 变成既有 `NormalizedModelDelta` 或 LangChain ordered content block，并执行 provider format self-check；它不读取 rollout/storage，也不从 canonical item 反推历史。
2. **pure mapping** 唯一归属 `app/services/mapping/itemized/`：它接收 domain DTO、Saver 返回的已提交 plan/selection 或 normalized block，执行 reasoning/message/attachment/agent-state/history 的确定性转换；不得访问数据库、文件、provider SDK 或 context reader，也不得写入 item/ToolSetRef。
3. **external provider request bridge** 唯一归属 `app/services/infrastructure/rollout_context/provider/toolset_request_bridge.py`：它把已校验的 `ToolSetRef`/tool-set manifest 按 provider capability 编码为 tools/tool-config/request fragment，并返回 capability loss；`app/agents/providers/` 的 wrapper 通过协议调用它，但不复制编码逻辑，`app/services/mapping/itemized/` 不实现它，任何其它 `infrastructure/provider` 文件也不得再定义同名投影。
4. **realtime orchestration** 只在 orchestration 层组装事件和推进内存状态；`stream_block_assembler` 的输出可以交给 ItemDraft/Saver adapter，但不能直接写 JSONL/SQLite。`agent_execution_service` 只决定何时调用 provider、tool、checkpoint/context port，不能把 provider response、canonical payload 或 storage outcome 放入自身状态模型。

上述四个目标目录的职责与现有 AGENTS.md 兼容：`app/agents/providers/AGENTS.md` 继续约束 wrapper/输出 block 规范化；`app/services/mapping/AGENTS.md` 继续禁止 I/O 和业务决策；`app/services/infrastructure/AGENTS.md` 继续约束可替换外部能力和 session path；`app/services/orchestration/AGENTS.md` 继续禁止持久化与映射。`app/agents/providers/`、`app/services/orchestration/`、`app/services/mapping/` 和既有 `app/services/business/session_turn_history/` 已有 AGENTS.md，不重复创建；本计划新增的 `app/services/mapping/itemized/`、`app/services/infrastructure/rollout_context/provider/` 以及前一节列出的新 source directory，必须在创建时各自增加四段式 AGENTS.md。

目标包的依赖只能向下流动：`app/domain/itemized` 只依赖标准库和明确的 JCS 实现；`app/services/infrastructure/rollout_context/storage` 依赖 domain contract、session path resolver 和 SQLite/文件 I/O；runtime、checkpoint owner 和 provider bridge 依赖 domain 与 infrastructure port；`app/services/mapping/itemized` 只依赖 domain/owner 返回的已提交 DTO，不访问 storage；`app/agents` 和 business service 只能依赖 Saver/Provider port，不能反向导入 JSONL writer、AppendWriter、SQLite reader 或 runtime ledger。`app/core/` 仅保留通用 path、ID、事件和一次性迁移基础设施；若某个新实现无法放入该依赖图，必须先调整归属，不得以跨层 import 绕过边界。

拆分不是“未来再决定”的开放项，而按以下顺序执行，每一步都必须在同一行为合同下保持可回滚；v2 是唯一生产运行时路径，不能以长期兼容为理由保留旧实现：

1. **冻结临时 shim 与目录规约**：先固定当前 SQLite/JSONL 公共表名、字段和 v2 import owner，创建目标目录及各自 `AGENTS.md`；提取期间若下游尚未迁移，旧路径只能提供带删除门槛的临时 import shim，且只转发到 v2 实现，不复制事实源、不读取 v1。
2. **提取纯 domain**：从 `itemized_rollout.py` 依次抽出 schema/enums → refs/selection/request-plan/assembly-snapshot → serialization → validation/compatibility → hashing/JCS；`plans.py` 只作为一次性拆分来源，完成 import graph 检查后删除其重复领域职责。v2 domain 不包含任何 v1 reader。v1 reader/report/quarantine 独立放入 migration 工具，只能由一次性 `legacy_import_v1_to_v2` 命令调用，禁止被正常 domain/runtime import。
3. **提取存储与恢复**：先把 `rollout_context/storage/service.py` 收敛为只负责依赖组装、port 路由和事务边界的薄 facade，再依次抽出 `storage/jsonl.py`、`catalog.py`、`commits.py`、`recovery.py`、`schema.py`，`checkpoint/view_anchor.py`，`execution/turns.py`、`executions.py`、`model_calls.py`，以及 `assembly/manifest.py`、`detail.py`、`overlay.py`。fork/compaction/pruning 进入独立 operation owner，history/projection 进入 mapping。这里的 v2 `storage/schema.py`/migration 只负责 v2 schema/data migration，不读取 v1 message-line；v1 reader/report/quarantine 固定归入独立的 `rollout_context/migration/`，只由一次性 import 命令调用。所有 storage reader 仍只通过已冻结 offset/catalog contract 暴露 v2 能力。
4. **提取 runtime**：从 `itemized_context_runtime.py` 按 detail store → ledger/composer → reconciliation → stream accumulator 顺序移动；先保留内存 ledger 与 sealed snapshot 的 owner boundary，再删除聚合实现内重复路径。
5. **提取 projection/history/provider**：先将无 I/O 的 selection/history/LangChain/reasoning/attachment/agent-state 映射移到 `app/services/mapping/itemized`，再把 ToolSetRef 外部 request bridge 固定到唯一的 infrastructure/provider owner；三者只能接收同一个 Saver-owned selection，provider wrapper 不得复制 bridge。
6. **拆分 provider 与实时 orchestration**：从 `litellm_content.py` 提取 response/stream normalization；从 `message_stream_runtime.py` 提取 trace observer、block/delta assembler、tool-call registry、terminal state machine；从 `agent_execution_service.py` 提取 step/control、retry/reminder、checkpoint/context adapter、tool/event transition。先保持 normalized block、实时事件和 durable item 的边界，再删除聚合文件中的重复转换。
7. **拆分 business service entrypoint**：从 `message_service.py` 提取 reasoning merge、message/attachment DTO、agent-state/history projection 到 mapping，将 cursor/page/service entrypoint 留在已有 session-turn-history business boundary；business 只能消费 Saver port，不得回流 item/storage 事实。
8. **迁移 Saver 与操作适配**：最后将 `checkpoint/saver.py` 与 `checkpoint/owner.py` 合并收敛为唯一 `context_owner.py` facade，并拆出 `persistence.py`、`view_anchor.py`、`fork_compaction.py`；assembly manifest/detail/overlay 不再由 Saver 或 storage 重复实现。核对 fork、rewind、compaction、replay 的依赖没有回流到 domain、orchestration 或直接读 storage。
9. **收尾与删除聚合实现**：在所有生产 import 已迁移到 v2、正常 history/provider/checkpoint/runtime 对 v1 访问审计为零、无 dual writer/dual projector/双 schema/双事实源、migration/import 仍能独立读取原始 v1 并完成报告/quarantine、相关未完成验收门槛全部闭合后，必须删除旧聚合职责和临时 import shim。删除是 7.5 和 change 完成的必要门槛，不是“有证据后再讨论”的可选事项；未完成的 1.4、4.4、4.7、6.1、6.5、6.6、7.x 不得因文件移动而勾选。

提取期间的临时 import shim 具有固定删除门槛和截止点：当对应目标模块、调用方 import、v2-only runtime smoke/静态 import 审计和 legacy migration 命令均可独立运行时，立即删除 shim；最迟不得晚于 7.5 最终验收、主 spec 同步和 change 完成之前。shim 不得暴露 v1 reader，不得让 normal history/provider/checkpoint 识别或投影 v1，也不得通过“旧读取开关”恢复旧运行路径。

每个新 source directory（包括 `app/domain/`、`app/domain/itemized/`、`app/services/infrastructure/rollout_context/`、其 `runtime/`、`storage/`、`checkpoint/`、`provider/`、`migration/` 子目录以及 `app/services/mapping/itemized/`）必须在创建同一目录时增加自己的 `AGENTS.md`，并包含项目约定的“目录用途”“可修改内容”“不可修改内容”“规范”四部分。该文件必须说明本目录的输入输出、禁止的 I/O/反向依赖、失败处理和验证命令；不能只依赖父目录说明。新目录下每个新增模块必须有单一职责和明确 import owner；不为已经存在的目录重复创建第二份 AGENTS，也不修改现有 `app/core/AGENTS.md`，而是通过上述归属消除冲突。

### 2. CanonicalItem 采用 typed core 加显式 extension

首版 canonical record 固定使用以下字段名和枚举边界；提取完成前，旧聚合文件中的类型定义只能作为迁移中的临时 import shim 指向 v2 类型，最终规范位置冻结为 `app/domain/itemized/schema.py`、`enums.py`、`refs.py`、`selection.py`、`request_plan.py`、`assembly_snapshot.py`、`serialization.py`、`validation.py` 和 `hashing.py`，`plans.py` 仅是一次性拆分来源，不是长期入口。拆分完成后必须删除旧路径中的重复定义和 shim，且不得让 v1 类型进入正常 runtime。v2 envelope 明确拆分为“必填核心字段”和“按语义可空的关联字段”，reader 不得把可空字段当成缺省语义：

```text
CanonicalItemRecord
├── format_version         # 必填非空，固定为 2
├── record_type            # 必填非空，固定为 item
├── item_sequence          # 必填非空，rollout 内物理顺序
├── item_id                # 必填非空，canonical 稳定身份
├── semantic_kind          # 必填非空，固定语义枚举
├── payload_kind           # 必填非空，text | structured_content | tool_call |
│                          # tool_result | summary | attachment_ref | opaque | extension
├── status                 # 必填非空，completed | partial | incomplete |
│                          # cancelled | failed | unknown；JSONL 写入后不可变
├── producer_ref           # 必填非空，单一 payload producer
├── payload                # 必填非空，typed payload/extension envelope
├── content_hash           # 必填非空，sha256:jcs:v1:<64位小写hex>
├── created_at             # 必填非空，创建时间
├── metadata               # 必填非空，至少是空 object
├── turn_id?               # 关联字段；仅属于某个 Turn 的 item 才填写
├── turn_scope?            # 关联字段；turn_root | turn_member |
│                          # ambient | pending_next_turn
├── message_group_id?      # 关联字段；仅用于 grouping，不改变 item 顺序
└── wire_role?             # 可选编码提示，不是来源或持久化分类
```

必填核心字段必须存在且非 null；`metadata` 即使没有值也必须写为 object，`producer_ref` 必须是完整的单一 producer reference，`payload` 必须携带与 `payload_kind` 匹配的值。`content_hash` 的算法、输入和编码也属于核心合同：先构造恰好为 `{ "payload_kind": <payload_kind>, "payload": <payload> }` 的对象，再按 RFC 8785 JCS 生成无空白 UTF-8 字节，计算 SHA-256，并以 `sha256:jcs:v1:` 加 64 位小写 hexadecimal 编码保存。文本按原始 Unicode 字符串哈希，不 trim、不换行归一化；对象 key 由 JCS 排序，数组保留 payload 的语义顺序；二进制必须先由 payload schema 明确编码为 base64url 等 typed value。item identity、sequence、semantic kind、status、producer、时间、metadata、Turn/group 关系、wire role 和 detail path 均不进入 `content_hash`，因此修改 envelope metadata 不会伪造正文变化。`turn_id`、`turn_scope`、`message_group_id` 和 `wire_role` 是可空/可省略的关联或投影字段。语义约束固定为：`turn_scope=turn_root` 必须同时有非空 `turn_id`，且该 item 必须是该 Turn 唯一的 `user_input` root；普通 Turn item 必须 `turn_scope=turn_member` 且有非空 `turn_id`；反向地，任何非空 `turn_id` 都必须配合 `turn_root` 或 `turn_member`，不得出现 `turn_id != NULL` 且 `turn_scope=NULL`；`turn_scope=ambient` 或 `pending_next_turn` 必须 `turn_id=NULL`，不能进入普通 Turn member/root 集合；`semantic_kind=runtime_notice` 的 pending notice 必须使用 `turn_scope=pending_next_turn`，而 request-only runtime notice 不产生 CanonicalItemRecord。若三个 Turn 关联字段均为 null，该 item 不属于任何 Turn，reader 不得从物理邻接、wire role 或 message group 推断其属于某个 Turn。`message_group_id` 为空不影响 item 的 canonical 顺序；非空时也不能改变上述 root/member/ambient 约束。

`semantic_kind` 是唯一的 canonical 语义枚举字段；`payload_kind` 只描述 payload 的物理/结构形态；禁止再使用无明确边界的 `kind`、`type` 或 `assistant_text` 作为 canonical item 语义分类。`assistant_text` 是从 `assistant_output` 的 text content part 生成的历史/live projection，`final_response` 是指向已选 final item 的 projection，不是 canonical semantic kind。

`CanonicalItemRecord.status` 的完整枚举固定为 `completed | partial | incomplete | cancelled | failed | unknown`，这些值在 JSONL 中全部表示终态事实；`completed` 表示该 semantic item 的声明 payload 已完整收敛、可按其 schema 正常投影，`partial` 表示已经持久化的 payload 是截至中断/停止边界的完整快照但尚未达到正常语义完成边界，二者都只能写成一个 immutable JSONL item，不能通过后续更新把 `partial` 改成 `completed`。`incomplete` 表示结构化 payload（例如 tool call arguments）没有完成，`cancelled` 表示被明确取消且没有继续执行，`failed` 表示已知失败，`unknown` 表示执行或结果无法确认。`open`、`active`、`running`、`draft` 和 `completed_empty` 不是 canonical item status：它们只能存在于内存 `ItemDraft` 或 assembly/Turn/control outcome；provider 空输出没有 item，使用 `Turn.status=completed_empty` 的 metadata-only terminal convergence。

ItemDraft 的唯一合法状态转移是内部 `draft -> completed|partial|incomplete|cancelled|failed|unknown`；canonical item 一旦写入 JSONL 就不再发生状态更新、删除、覆盖、重排或原地修复，任何后续更正、retry 或 resume 都必须追加新的 `item_id`/`item_sequence`，并用 `supersedes`、`retry_of` 或 `resumes` relation 连接旧 item。只有 terminal status 的 draft 才能进入 JSONL durability barrier；崩溃时没有足够稳定 payload 的 draft 不得补造 `status=unknown` item，只能由独立的 control/assembly outcome 记录 execution lost。`status=completed` 才能作为正常完成事实，其他五种状态都不得被历史或 `final_response` projection 当作成功输出。`CanonicalItemRecord.status` 与执行/控制结果属于不同字段域：后者统一使用 `ControlOutcome=completed|completed_empty|failed|interrupted|cancelled|execution_lost|unknown`，只允许出现在 `ExecutionRecord`、`ModelCallRecord`、assembly、storage commit 或控制记录的 outcome 字段；不得引入带 outcome 前缀的 unknown 状态别名。工具结果的 payload 可以额外携带 typed `tool_outcome=success|failure|cancelled|unknown`，它不是 item status；`tool_outcome=unknown` 或任何非 success 结果都不得作为成功 replay input。

`payload_kind` 的完整枚举固定为：`text`（精确 Unicode 字符串）、`structured_content`（按已知 payload schema 编码的 JSON object/array）、`tool_call`（规范化工具调用参数结构）、`tool_result`（规范化工具结果结构）、`summary`（摘要结构）、`attachment_ref`（附件的稳定引用及长度/hash 元数据）、`opaque`（显式编码的不可解释或受保护值）和 `extension`（扩展 envelope）。`opaque` 至少包含非空 `encoding`、`value`、provider/wire type 和 schema version；`extension` 的 `extension_schema` 与 `extension_version` 都是必填非空字符串，其中 schema 是稳定的 namespaced schema identifier，version 是该 schema 的显式版本值，此外还必须有 `value` 和 protection/encoding metadata，扩展 value 仍必须能按该 schema 做确定性 JCS 编码。`semantic_kind=extension` 必须使用 `payload_kind=extension|opaque`；其它 semantic kind 必须通过固定的 semantic/payload compatibility table 校验，不得用 `structured_content` 静默包裹未知 payload。

semantic/payload/status compatibility table 是 v2 的闭合合同；表中未列出的组合一律非法：

| `semantic_kind` | 允许的 `payload_kind` | 允许的 `CanonicalItemRecord.status` | 额外 marker/约束 |
|---|---|---|---|
| `user_input` | `text`, `structured_content` | `completed` | `turn_root` 必须是唯一 user root；不得有 `tool_outcome` |
| `assistant_output` | `text`, `structured_content` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 只有 `completed` 可参与 finalization；不得有 `tool_outcome` |
| `reasoning` | `text`, `summary`, `opaque`, `extension` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | `opaque`/`extension` 必须有 protection/encoding metadata；不得作为 final item |
| `tool_call` | `tool_call`, `structured_content` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 必须有 tool invocation/call identity；不得用 `tool_outcome` 表示 call status |
| `tool_result` | `text`, `structured_content`, `tool_result`, `opaque`, `extension` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | payload 必须带 tool attempt/result identity；`status=completed` 时 `tool_outcome` 可为 `success|failure|cancelled|unknown`，但若外部执行结果未确认必须为 `unknown`；其它 status 只能省略 marker 或使用 `unknown`；只有 `status=completed` 且 `tool_outcome=success` 才可 replay |
| `runtime_notice` | `text`, `structured_content`, `opaque`, `extension` | `completed` | 持久化 pending notice 必须是 `pending_next_turn` 或 `ambient` scope；失败由 ControlOutcome 记录，不补造 item；不得有 `tool_outcome` |
| `compaction_summary` | `summary`, `structured_content` | `completed` | 必须绑定 compaction/view revision；失败由 ControlOutcome 记录，不补造 item；不得有 `tool_outcome` |
| `attachment` | `attachment_ref` | `completed` | payload 必须含稳定 ref、长度和 hash/availability；不可用附件仍用 ref metadata 表达，不得用 `tool_outcome` |
| `extension` | `extension`, `opaque` | `completed`, `partial`, `incomplete`, `cancelled`, `failed`, `unknown` | 必须有非空 `extension_schema`/`extension_version` 和 protection metadata；扩展自定义 outcome 必须 namespaced，核心不得解释 |

`CanonicalItemRecord.status` 只描述该 item payload 的持久化/语义完成事实；`ControlOutcome` 只描述 execution、model call、assembly 或提交控制事实，二者即使出现相同字符串也不得互相推导。校验器在 JSONL durability barrier 前必须检查整行的 semantic/payload/status/marker 组合；非法组合返回 `item-schema-incompatible`，不写 JSONL、不写 item catalog、不推进 offset。未知 `semantic_kind`、`payload_kind` 或非 namespaced marker 进入 format/schema recovery error，不能降级为 `opaque`、`unknown` 或普通文本；已知 extension 无 handler 时按既有 unsupported extension 规则保留原文但禁止普通 context projection。

所有 execution、model call、assembly、storage commit 和控制记录的结果字段必须命名为 `outcome`，其值只能来自 `ControlOutcome=completed|completed_empty|failed|interrupted|cancelled|execution_lost|unknown`；`unknown` 表示该控制结果无法确认，不能改写成另一个 item status 名称。`tool_outcome` 只存在于 `tool_result` 的 typed payload，并且只能按兼容性矩阵取值；它不能出现在其它 semantic kind，也不能替代 `outcome`。因此 item `status=unknown`、控制记录 `outcome=unknown` 和 tool payload `tool_outcome=unknown` 是三个不同字段域，reader 不得通过字段名或字符串值互相推导。

v2 reader 遇到未知 `payload_kind`、缺失必填 extension schema/version、非法 payload shape 或不支持的 semantic/payload 组合时必须进入明确的 format/schema recovery error；不得自动降级成 `opaque`、空 payload 或普通文本。已知 `payload_kind=extension` 但 extension schema/version 没有可用 handler 时，可以保留原始 immutable JSONL、hash、offset 和 unsupported metadata，但不得进入普通 context projection；若该 item 是请求必需来源，则返回 `extension-unsupported`。新增 payload kind 必须通过新的 format dispatch/schema version 承认，不能在 v2 reader 中静默接受未知字符串。

v2 的 JSONL 顶层 envelope 字段名在本 change 内冻结，不由后续 schema review 改名：必填非空字段为 `format_version=2`、`record_type=item`、`item_sequence`、`item_id`、`semantic_kind`、`payload_kind`、`status`、`producer_ref`、`payload`、`content_hash`、`created_at`、`metadata`；可空关联字段为 `turn_id`、`turn_scope`、`message_group_id` 和 `wire_role`。SQLite manifest/database metadata 的 `rollout_format_version=2` 必须与每行 `format_version=2` 和 `record_type=item` 一致。v1 legacy envelope 对应固定的 `format_version=1`、`record_type=message`、`message_sequence`、`message_id`、`turn_id`、`role`、`message` 和 `metadata`；v1/v2 不得在同一个 rollout 中混写。`rollout_format_version` 是存储 dispatch 字段，`format_version` 是每行协议字段，两者职责不同但必须一致。

`item_sequence` 只负责物理线性顺序；`message_group_id` 只负责把多个 item 编译到一个目标 message，不能用于删除或重排 item。Provider item id、canonical item id、tool call id、tool execution id 和 target message id 分开保存。reasoning 的 readable text、summary、encrypted/opaque body 和本地 presentation state 也分开保存。

v2 `rollout.jsonl` 是 append-only 的 immutable item fact log：一行必须包含一个完整、已终态化的 `CanonicalItemRecord`，行内 UTF-8 字节范围和 `item_sequence` 在提交后永久不变；不得通过修改旧行改变 status/payload/hash、删除旧行、插入中间行或重排既有行。JSONL 行先完成 durability barrier，再由 SQLite `storage_commits` 和 `item_catalog` 宣布可见；只有已提交 offset 以内且有 catalog/commit 记录的行对 reader 可见。JSONL 尾部若已写入但 SQLite 未提交，只能作为未收敛尾部回收或隔离，不能被 reader 猜测为事实。对已提交事实的修正只能追加新 item 并建立显式 relation；SQLite projection/index 也不得反向覆盖 JSONL payload。

每个 draft、contribution、assembly 和终态 item 还关联不影响 payload 语义的 provenance。`producer_ref` 只描述谁产生了 payload；middleware、memory、environment source 等没有产生该 payload 时，必须作为独立的关系边记录，而不是冒充 item producer：

```text
producer_ref
├── producer_kind     # user | provider | middleware | tool | system | runtime
├── producer_id       # 稳定 producer identity
├── invocation_id?    # 本次 source/middleware/model/tool 调用
└── source_version? / source_hash?

provenance_edge
├── relation         # influenced_by | transformed_by | derived_from |
│                    # summary_of | replaces | notice_for | causes |
│                    # result_of | retry_of | resumes | replay_input
├── source_ref       # item/contribution/source/assembly reference
├── target_ref       # item/contribution/assembly reference
├── produced_order?
├── visibility/protection
└── detail_ref?      # 受保护的扩展详情引用
```

`producer_ref` 是每个 canonical item 的单一 payload 产生者；多来源影响必须通过 `provenance_edge` 表达，不能把多个 middleware 填进 `producer_ref`。`produced_by` 只允许作为 source-to-item 的审计关系，不作为 item 上的第二个 producer 字段。provenance 不是把 middleware 内部对象整个序列化进 item。它先存在于当前 Turn 的内存 `ContextAssemblyLedger`/`ItemDraft`，item 提交时只复制稳定 identity、版本/hash、关系和详情引用；这样实时事件能立即关联来源，重启后又能通过 SQLite 恢复最小可用 metadata。若 middleware 只是影响 system/developer 输入而没有产生历史 payload，关系目标应是该 `ContextAssemblySnapshot` 或 `RequestItemRef`，而不是 assistant item 的 `producer_ref`。

tool 因果使用独立的逻辑 invocation、实际 call 和执行 attempt identity，不把 provider 的 `tool_call_id` 直接当成全部重试/恢复语义：`tool_invocation_id` 是一个逻辑工具请求在 session 内的不可变 identity；每个实际 model-declared call attempt 有唯一 `tool_call_id` 和一个 `semantic_kind=tool_call` item；每次实际工具执行有唯一 `tool_attempt_id`，并通过 `causes`/`result_of` 关系连接 call 与 result。约束是一个 `tool_invocation_id` 可以有多个按序 call attempt，一个 call attempt 至多启动一个 tool attempt，一个 tool attempt 至多有一个已提交 `tool_result` item；缺少 result 表示尚未完成或 unknown，不得从最后一条 tool message 猜测成功。

`tool_call_id`、`tool_attempt_id` 和 `tool_invocation_id` 都在 session 内唯一；tool attempt 的执行幂等键必须包含 `tool_attempt_id` 和输入 payload hash，同 key 重试相同 payload 只返回已有 outcome，不同 payload 报冲突。Provider retry 或 execution resume 产生新的 call/attempt identity，并以 `retry_of`/`resumes` 关系连接旧 identity；旧 call/result immutable，新的结果不能覆盖旧结果。只有与当前 active view/lineage 关联、`status=completed` 且 tool attempt outcome 为 success 的 `tool_result`，才可以通过 `replay_input` 关系作为后续 assembly 的自动 replay 输入；superseded、failed、partial 或 unknown result 默认不能作为 replay 输入，必须显式重新执行工具或报告不可重放。

typed core 只覆盖可跨 provider 验证的语义。无法解释的字段使用 `extension`，至少携带 provider、wire type、schema version 和保护/脱敏状态；禁止用一个万能 JSON 字段掩盖未知语义。

Turn 是一次用户交互的业务边界，不是 LangChain message、Provider model call 或 wire role。系统在接受真实用户输入时创建 `TurnRecord`，并在同一提交边界内创建其 root input item：

```text
TurnRecord
├── turn_id
├── turn_ordinal              # session 全局 acceptance ordinal，不随分支重排
├── accepted_ingress_id       # 被接受的外部输入 identity
├── acceptance_idempotency_key # session 内 acceptance 幂等键
├── root_input_item_id        # 唯一的 Turn 起点
├── root_input_item_sequence
├── initial_execution_id      # acceptance-time 创建的首次 execution
├── last_execution_id?        # 最近一次关联 execution，便于快速定位
├── final_item_id?            # 仅 completed Turn 可有
├── last_item_id?
├── status                    # open | active | completed | completed_empty |
│                             # interrupted | cancelled | failed | unknown
└── source_branch_id          # 首次接受该 Turn 的 origin branch
```

`Turn.status` 的闭合集合固定为 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`；其中 `open` 与 `active` 是非终态，`completed`、`completed_empty`、`interrupted`、`cancelled`、`failed` 与 `unknown` 是 terminal outcome。`completed_empty` 是唯一的“请求正常结束但没有 canonical output item”名称，不能改写为 `empty`、`no_output`、`empty_completed` 或其它同义状态，也不是 `CanonicalItemRecord.status` 的枚举值。合法转移固定为：`open -> active|cancelled|failed|unknown`；`active -> completed|completed_empty|interrupted|cancelled|failed|unknown`；`interrupted -> active` 仅允许显式 resume 且必须创建新的 `execution_id`，`unknown -> active` 仅允许其 reason=`execution_lost` 时显式 resume 且必须创建新的 `execution_id`；其它 terminal outcome 不得再转移。每次转移与对应 execution/control outcome 必须在同一 SQLite 收敛事务中可见；Provider retry 在 `active` 内只创建新的 model-call/attempt，不改变 Turn status。

| 当前 `Turn.status` | 允许的下一状态 | 条件 |
|---|---|---|
| `open` | `active`, `cancelled`, `failed`, `unknown` | acceptance 已提交后只能由执行启动或明确控制结果收敛 |
| `active` | `completed`, `completed_empty`, `interrupted`, `cancelled`, `failed`, `unknown` | provider/工具/控制结果在 terminal convergence 中一次提交 |
| `interrupted` | `active` | 仅显式 resume；必须新建 `execution_id`，无新用户输入继续原 Turn |
| `unknown` | `active` | 仅当 reason=`execution_lost` 且显式 resume；必须新建 `execution_id`，其它 unknown 不可恢复 |
| `completed` | 无 | terminal；`final_item_id` 必须已确定 |
| `completed_empty` | 无 | terminal；无 canonical output，`final_item_id=NULL` |
| `cancelled` | 无 | terminal；对该 Turn 的 `resume_turn` 或绑定原 `turn_id` 的 `dispatch_replay` 均返回 `turn_not_resumable`；新执行只能调用独立的 `replay_as_new_turn` |
| `failed` | 无 | terminal；只能由新的真实用户输入创建新 Turn |

回放操作的 API 语义固定分离：`history_replay` 只生成历史 projection，不创建 execution；在同一 owner session 内，它可以让新的 history view 复用 source `turn_id`、`root_input_item_id` 和既有 Turn lineage。`resume_turn` 只对状态表允许的 `interrupted` 或 reason=`execution_lost` 的 `unknown` 复用原 Turn 并创建新 execution；`dispatch_replay` 表示把 Provider dispatch 绑定到原 `turn_id`，对任意 `cancelled` Turn（包括普通 cancelled 与 `full_rollout_copy` 的 cancelled historical）必须返回 `turn_not_resumable`，不得写入新 execution 或修改原 Turn。`replay_as_new_turn` 才是重新执行能力的独立显式新 Turn 创建操作：它可以把 source history 作为上下文前缀引用或复制，但其 active view 必须登记新的 target-local `TurnRecord`、新的 `user_input` root、accepted ingress、acceptance、initial execution 和新的 `context_view_turns.logical_turn_ordinal`；source Turn/root 只能作为前缀或 lineage，不能同时成为新 Turn 的 root。`replay_as_new_turn` 内部随后可以发生绑定新 Turn 的 Provider dispatch，但它不是原 Turn 的 `dispatch_replay` 或 `resume_turn`，同一个 API 请求不得一边按 `dispatch_replay` 返回错误、一边创建新 Turn。

`cancelled` 是吸收态，普通 cancelled Turn 与 `full_rollout_copy` 为未复制 source runtime 写入的 cancelled historical（reason=`fork_source_runtime_not_copied`）采用相同规则：`resume_turn` 与绑定原 `turn_id` 的 `dispatch_replay` 永久返回 `turn_not_resumable`，不创建 execution/model-call、不修改 status；`history_replay` 只能生成 projection，不得 dispatch。若调用方需要重新执行，必须明确调用独立的 `replay_as_new_turn`，由该新 Turn 创建操作分配 target-local Turn/root/acceptance/initial execution，并用 `replay_of_turn_id` 保存 lineage；它不是原 cancelled Turn 的 dispatch replay，也不能在同一 API 语义中同时返回错误并创建新 Turn。需要继续交互时也只能接受新的真实用户输入并创建新的 Turn。

`root_input_item_id` 必须指向 `semantic_kind=user_input`、`producer_ref.producer_kind=user` 的 canonical item。Turn 的开始不得根据 `wire_role=user`、第一个物理 item、`message_group_id` 或 provider call 推断。`turns` 表保存 root item 的稳定 identity/sequence，`context_view_turns` 另外保存该 Turn 在 view 中的 `logical_turn_ordinal` 和 root item 引用；因此按 Turn 起点查询不需要扫描 item 或解析 wire message。

`accepted_ingress_id` 和 `acceptance_idempotency_key` 各自在 `(session_id, accepted_ingress_id)` 与 `(session_id, acceptance_idempotency_key)` 范围内唯一，并且各自只能一对一指向一个 accepted Turn；它们不是跨 session 的裸全局 ID。相同 acceptance key、相同 accepted ingress、相同 ingress payload hash 和相同 source branch 的重试必须返回原 `turn_id`、root item 和 `initial_execution_id`，不得创建第二个 root。相同 key 但 ingress ID、payload hash 或 source branch 不同，或相同 ingress ID 但 key/payload 不同，都必须返回明确的 acceptance idempotency conflict，并保持原 Turn 不变；冲突检查与 Turn/root/initial execution 创建必须在同一 SQLite 事务中完成。acceptance-time 的 Turn、root `user_input` item 和首次 execution 必须处于同一个可重试的提交边界。

`system_reminder` 这类运行时提示如果需要持久化，保存为 `semantic_kind=runtime_notice`、`payload_kind=text` 的 canonical item，`turn_id` 可以为空，`turn_scope=pending_next_turn`，并通过 `notice_for` 或 `related_execution_id` 关联被中断执行及后续 assembly。它不会创建 normal Turn，也不会占用下一个 Turn 的 root 位置。下一条真实用户输入再创建新的 `TurnRecord`；如果只是无新用户输入且 Turn.status 转移表允许的 `resume_turn`，则继续原 Turn，并创建新的 execution/model-call identity。对 `cancelled` Turn 必须返回 `turn_not_resumable`；重新执行只能调用独立的 `replay_as_new_turn`。

Turn、execution 和 model call 使用不同的 identity 层级：

```text
TurnRecord
  └── TurnExecutionLink               # (turn_id, execution_id) 唯一关联
        └── ExecutionRecord            # 一次 AgentLoop 运行
              └── ModelCallRecord      # 一次 Provider 请求
                    └── CanonicalItemRecord # provider/tool/runtime 产出的事实
```

`TurnExecutionLink` 至少保存 `turn_id`、`execution_id`、`execution_ordinal`、`relation=initial|resume|replay|retry` 和 link idempotency key，并以 `(turn_id, execution_id)` 唯一；`ExecutionRecord` 至少保存 `execution_id`、`turn_id`、`execution_ordinal`、`accepted_ingress_id?`、`resumes_execution_id?`、`replay_of_execution_id?`、`first_model_call_id?`、`last_model_call_id?` 和 outcome/status。`ModelCallRecord` 至少保存 `model_call_id`、`execution_id`、`attempt_ordinal`、`retry_of_model_call_id?`、`assembly_id`、`dispatch_state`、`provider_request_id?` 和 outcome。`turn_id` 到 execution 以及 execution 到 model call 只能通过这些显式关联读取，不能由 item 的物理顺序推断。

接受用户输入时，`TurnRecord`、root `user_input` item 和首次 `ExecutionRecord` 必须使用同一幂等提交边界建立；重复提交同一 ingress idempotency key 不得创建第二个 root。一次 execution 内的 Provider retry 创建新的 `model_call_id`，保留 `retry_of` 和 attempt ordinal；如果整个 AgentLoop 因崩溃或 resume 重新启动，则创建新的 `execution_id`，保留 `resumes_execution_id`/`retry_of` 关系，但继续使用原 Turn。任何 retry/resume 都不得复用已经提交的 output item id。

`TurnRecord.final_item_id` 只在 `turn_finalize` 与 canonical output item 同一 terminal convergence 提交边界内写入，并且必须指向同一 `turn_id` 下明确的 `semantic_kind=assistant_output`、`status=completed` canonical item。`Turn.status=completed` 时 `final_item_id` 必须非空；`completed_empty`、`open`、`active`、`interrupted`、`cancelled`、`failed` 和 `unknown` 时必须为空。`cancelled` 只表示显式不再运行该 Turn，不能被投影为成功；`assistant_text` 和 `final_response` 都是 projection；没有成功 finalization 时，不能把“最后一条 assistant item”当作最终响应。Provider 空输出使用 `completed_empty`，不伪造 output item 或 final item。

分支和回放对上述字段采用以下冻结语义：`turn_id` 是 session 内逻辑用户交互的全局不可变 identity；`turn_ordinal` 是首次 acceptance 分配的 session-global ordinal，历史 Turn 在派生 branch/view 中复用原值，不因分支重排；`source_branch_id` 是首次 acceptance 所属的 origin branch，历史 Turn 被复制到新 branch/view 时仍保留原值。只有 fork 后真正接受新的用户输入，或显式调用 `replay_as_new_turn`，才创建新的 `turn_id`、新的 `turn_ordinal` 和以新 branch 为 `source_branch_id` 的 Turn。`resume_turn` 只对状态表允许的 Turn 继续原 `turn_id` 并创建新的 execution/model-call lineage；`history_replay` 是唯一可以在同一 owner session 内复用 source Turn/root 的回放操作，且不创建 execution；`replay_as_new_turn` 必须创建新的 Turn、独立的 root/acceptance/initial execution，并为该新 Turn 在 active view 登记新的 `logical_turn_ordinal`，source history 只能作为前缀或 lineage。加载、复制 view 或恢复 checkpoint 本身不得隐式创建 execution；它不能被实现为对原 Turn 的 `dispatch_replay`。

`context_view_turns.logical_turn_ordinal` 是 view-local 的唯一排序，唯一约束为 `(view_id, logical_turn_ordinal)`，同一 `turn_id` 在不同派生 view 中可以有不同逻辑序号。`context_view_turns` 必须同时保存 `view_id`、`turn_id`、`logical_turn_ordinal`、`root_input_item_id` 和 fork lineage reference；root lookup 先验证 view lineage，再由 `turn_id` 取 `TurnRecord.root_input_item_id`，不能使用 `MIN(item_sequence)` 或 view 中第一条 wire message。`history_replay` 创建的 history view 可以登记 source Turn/root 的既有 identity 和该 view 的 logical ordinal；`replay_as_new_turn` 创建的 active view 必须另登记新 Turn、新 root 和新的 logical ordinal，source history 只能作为前缀引用/复制，不能把 source root 写入新 Turn 的 `root_input_item_id`。fork 采用 copy-on-write reference，不复制 Turn/item 正文；新 view 的顺序只由 logical ordinal 表达，global `turn_ordinal` 不参与 view 内重排。

上述 identity 复用只适用于同一 session 内的 branch/view。跨 session fork 使用显式复合引用 `GlobalEntityRef=(session_id, entity_type, local_id)`；`session_id` 是物理会话数据的 owner namespace，`local_id` 在 owner session 内唯一。所有 Turn、item、tool invocation/call/attempt、execution、model call、assembly、checkpoint、view、branch、operation anchor、source overlay 和 detail 的跨 session 引用都必须携带 source session namespace，不能只传裸 ID。

`afork(source_session_id, target_session_id, mode)` 成功后，target session 必须完全自持其可运行数据：目标会话为每个复制的实体分配新的 target-local ID，并在 `fork_entity_mappings`/等价 provenance 中保存 source `GlobalEntityRef` 到 target `GlobalEntityRef` 的一对一不可变映射。source ID 可以作为 `legacy_source_ref`/lineage metadata 保留，但不得作为 target active view 的裸 canonical identity；source assembly、tool identity、execution/model-call identity 也不得直接复用。target 的 `root_input_item_id`、`item_sequence`、JSONL offset、Turn ordinal、tool relation 和 assembly refs 全部指向 target namespace；source sequence/offset 只能作为审计坐标，不能被 target reader 打开。
Turn 的 `accepted_ingress_id` 与 `acceptance_idempotency_key` 也必须做同样的一对一 target-local mapping，分别满足 target session 内各自的唯一约束，并在 mapping 中标记 `identity_origin=fork_copied`；source acceptance identity 只保留为 source `GlobalEntityRef`/lineage。复制后的 target acceptance identity 不能被普通新输入重用，target 新输入必须产生新的真实 ingress/key；复制 Turn 的显式 `resume_turn` 只有在 Turn.status 转移表允许时才复用 target acceptance 并创建新的 target execution/model-call；`history_replay` 不创建 execution，`replay_as_new_turn` 才创建新的 target Turn/root/acceptance/initial execution 并以 `replay_of_turn_id` 关联，且不属于原 Turn 的 `dispatch_replay`；普通或 historical `cancelled` Turn 的 `resume_turn`/`dispatch_replay` 均返回 `turn_not_resumable`。重复同一 fork idempotency key 返回既有映射，mapping 或 target acceptance identity 不一致必须报冲突。

source overlay 也必须 target-local：每个被复制的 `source_overlay_epoch` 在 target 重新编号为 target-local epoch，source epoch 只保留在 `fork_origins`/overlay lineage；base、delta、supersedes/materializes relation 和其 canonical ambient item 必须建立 target mapping。需要重放的 sealed assembly 所引用的 detail 必须复制到 target Session 的目标 main thread node内 `rollout/context-plan-details/<target-assembly-id>/<target-detail-id>`，并换成 target-local `detail_id`/`detail_ref`；detail 不得通过 source path 共享。detached target 在 fork 提交后完全不依赖 source，source 删除不会影响 target；pinned 只为审计和保留 source lineage 在 source 建立 retention reference，target request 仍只读 target-local overlay/detail。可选 detail 若因权限或 retention 无法复制，target 记录 unavailable；required detail 无法复制则 fork 失败，不得留下半可运行 target。

三种跨 session fork 的物化范围固定为：`context_fork` 复制 source active view 的有效 canonical item/Turn/checkpoint prefix；`history_prefix_fork` 复制从 source session 开始到指定 inclusive/before anchor 的有效 prefix；`full_rollout_copy` 复制 source rollout 的全部 canonical records、SQLite checkpoint/view/branch/control state、checkpoint channel state 和可复制的 overlay/detail lineage，并为全部实体建立 target 映射。source v1 的原始 message-line 若需保留，只能由一次性 `legacy_import_v1_to_v2` staging 作为 migration/rollback audit 输入保存；它不挂载到 target 正常 reader，也不构成 target 的 legacy runtime。三种模式都创建新的 target branch/view；`full_rollout_copy` 的“full”描述语义范围，不意味着 source 和 target 可以共享 SQLite、JSONL 或 namespace。target active view 使用 target-local logical ordinal，source view/branch/turn ordinal 仅保留在 fork lineage。无论 source 是 v1 还是 v2，跨 session fork 的 target 一律是 `rollout_format_version=2`；v1 只能通过上述一次性 migration staging 映射到 target-local v2 item/Turn/execution，源 `message_id`、`message_sequence`、offset 只写入 `legacy_source_ref`，不能成为 target identity 或 offset。

复制的 Turn 使用 target 新 `turn_id`、target 新 root item 和在 target session 内保持相对顺序的新 `turn_ordinal`；source `turn_id`/ordinal/origin branch 只写入 source lineage。复制的 tool call/result 必须使用 target tool invocation/call/attempt IDs 和 target relation；只有映射后、当前 target active lineage 中成功 completed 且未 superseded 的 target result 才能作为 replay input。复制的 sealed assembly 使用 target 新 `assembly_id`，保留 source assembly ref、plan/request hash 和 detail availability；active/未终态 assembly 不得自动恢复为可运行请求。`context_fork` 与 `history_prefix_fork` 在 preflight 发现选定范围含未完成 Turn/active execution/未终态 assembly 时直接拒绝，目标 session 不创建；`full_rollout_copy` 则允许目标创建完整历史副本，但把对应 target Turn 标记为 `cancelled`（reason=`fork_source_runtime_not_copied`），其 target execution/model-call/assembly 仅为不可运行的历史状态，不自动 resume 或伪造成功。source 运行态不会以“复制后取消”方式改变 source 状态。

跨 session fork 的 source/target retention 和 lineage 也分离：target `fork_origins` 保存 source/target session、source checkpoint/view/branch、mode、mapping version、source overlay epoch/detail mapping 和 relationship；detached fork 在 target 物化提交后不依赖 source，pinned fork 才在 source `retention_refs` 保留 source 的 lineage/detail，target 删除时释放。target 的 active view、checkpoint 和历史 API 永远只读取 target namespace；新用户输入或显式 `replay_as_new_turn` 在 target active branch 创建新的 target-local Turn/root/acceptance/initial execution，且 target `turn_ordinal` 大于已复制 Turn 的最大值。`replay_as_new_turn` 可以把已映射的 source history 作为 target-local 上下文前缀引用或复制，但 target active view 必须另登记新 Turn 的新 `root_input_item_id`、acceptance、initial execution 和新的 `logical_turn_ordinal`，source Turn/root 不能成为该新 Turn 的 root；对复制后的 target Turn，只有状态表允许的显式 `resume_turn` 才能复用 target `turn_id` 并创建新的 target execution/model-call/assembly；`history_replay` 不创建 execution，且仅在同一 owner namespace 的历史 view 中复用 source Turn；`replay_as_new_turn` 创建新的 target Turn/acceptance 并以 `replay_of_turn_id` 关联，不得把它解释为原 Turn 的 `dispatch_replay`；普通或 historical `cancelled` Turn 的 `resume_turn`/`dispatch_replay` 均返回 `turn_not_resumable`。fork 的 source prefix cutoff 不会自动删除 target overlay；下一次 reconciliation 决定复用 target base/delta、追加 delta 或物化 target base。

### 3. JSONL 保存 canonical item，SQLite 按用途分层保存索引和 view

JSONL 每行保存一个 item envelope，包含可独立校验的版本、identity、payload 和 hash。SQLite 不把所有 item 都做成同样宽的 message 表，而是按用途分层：

```text
item_catalog                 # 每个已提交 canonical item 必有，最小恢复/定位索引
item_relations               # 仅有 parent/group/tool/source 等关系时建立
item_projections             # 仅历史摘要、详情或搜索需要时建立
item_parts                   # 仅需要 part/fragment 级读取或 anchor 时建立
operation_anchors            # 仅可 interrupt/rewind/compaction/fork 的边界
context_assemblies           # 每次 model call 的 immutable plan + lifecycle metadata
assembly_item_refs           # assembly 使用过的 canonical/request-only 引用
storage_commits              # item-bearing 与 metadata-only 的提交账本
```

所有已提交 canonical item 都必须有 `item_catalog` 行，以保证 committed offset、hash 校验、顺序恢复和未来审计；但内部 opaque item、仅参与一次请求的 item 或不展示的 item 不强制建立完整正文 projection、source detail 或 operation anchor。临时 prompt contribution 和 tool definition 如果没有被声明为持久化事实，根本不进入 JSONL item catalog，只在 assembly metadata 中保留引用/hash。

`item_catalog` 至少包含 item identity、物理顺序、`semantic_kind`/`payload_kind`/status、可选的 Turn/group 关联、JSONL offset/length、content hash、commit identity、visibility 和可选的 provenance summary/ref；对真实 Turn member 建立 `(turn_id, item_sequence)` 查询索引。`turns` 记录 `root_input_item_id`/`root_input_item_sequence`，`context_view_turns` 记录 root 引用和 view 内逻辑顺序。`item_relations` 将 tool call/result、parent、supersedes、source 和 group 关系从 catalog 中拆出，避免让每个 item 都携带完整关系列。`item_projections` 保存有界 text/summary、reasoning flags、tool summary 和 detail availability；`item_parts` 是可选的派生稀疏索引，只为需要细分读取的 payload 建立 part identity/ordinal/locator/hash，不是 canonical 正文来源。

为使 `ContextRef` 可恢复，v2 `item_catalog` 还必须保存逻辑 `payload_length` 和非空 `source_revision`：前者是按 payload schema 编码的正文 bytes，后者是提交时固定的 source revision token；没有外部 source 时由 writer 生成 `canonical:<item_id>:<content_hash>`。这两个字段与 `content_hash` 一起组成 ref integrity manifest，不能由 JSONL line offset/length、当前文件或 wire message 长度替代。

`content_part` 的 canonical 来源已经冻结为父 item JSONL envelope 的 `payload`；多 part payload 必须按 payload schema 在 `payload.parts`（或同等明确字段）中保存稳定的 `content_part_id`、semantic kind、ordinal 和 part value，part value 与 `payload_kind` 一样参与父 item 的 `content_hash`。SQLite `item_parts` 只能保存 `item_id`、`content_part_id`、ordinal、part semantic kind、JSON Pointer/part ordinal locator、可选的 JSONL line byte offset/length、父 line hash 和 part hash/prefix hash，用于稀疏定位；这些 offset 是相对 immutable UTF-8 JSONL 行的加速坐标，必须用父 line hash/content hash 校验，不能覆盖或替代 payload。大内容仍从 JSONL 按 item offset/length 有界读取；`item_parts` 缺失时可以解析目标 item，索引冲突时必须报告 integrity error，不能猜测或读取 detail store。`ContextPlanDetailStore` 永远不承载 canonical content part 或正文第二副本。

写入沿用当前 rollout 的跨文件收敛原则，但把“准备发请求”和“请求结束”分成两个不同的提交阶段。SQLite `storage_commits` 是控制提交账本，至少保存 `commit_id`、`commit_kind`、`commit_mode`、`subject_id`、`idempotency_key`、`jsonl_offset_before`、`jsonl_offset_after`、`jsonl_record_count`、`database_transaction_id` 和 commit outcome。一个逻辑 SQLite 事务只写一行 `storage_commits`；该行可以覆盖同一批次的多个 JSONL item，所有 item catalog 行指向这个 commit。

```text
storage_commits.commit_kind
├── acceptance                 # root item + Turn + initial execution
├── assembly_sealed             # sealed-before-dispatch
├── item_convergence            # 非 terminal 的 canonical item 批次
└── terminal_convergence        # assembly/execution/model-call/Turn 终态

storage_commits.commit_mode
├── item_bearing               # jsonl_record_count >= 1
└── metadata_only               # jsonl_record_count = 0
```

`commit_kind` 和 `commit_mode` 是正交字段，不得把 `metadata_only` 写入 `commit_kind`。`acceptance` 必须是 `item_bearing`，且同一事务建立 root item、Turn 和 initial execution；`assembly_sealed` 必须是 `metadata_only`，因为它只封存 assembly/control state；`item_convergence` 必须是 `item_bearing`；`terminal_convergence` 可以是 `item_bearing`（canonical item 与终态一起收敛）或 `metadata_only`（空输出、失败、中断、执行丢失等无 item 终态）。`(session_id, commit_kind, subject_id, idempotency_key)` 必须唯一：同一 acceptance subject 只能有一个成功 acceptance，同一 assembly/model-call 只能有一个成功 seal，同一 item batch 只能有一个成功 item convergence，同一 terminal subject 只能有一个成功 terminal convergence；同 key 重放时 payload、outcome、mode、record count 或 offset span 不同都必须报冲突。`jsonl_offset_before`/`jsonl_offset_after` 是 rollout 文件的绝对字节边界，不是 item sequence，也不是事件计数。每个 commit 都必须记录这两个值；metadata-only commit 的 `jsonl_record_count=0` 且 `jsonl_offset_after=jsonl_offset_before`，不能为了填 offset 伪造空 item。只有真实 JSONL item 已经完成 durability barrier 时，offset 才可以前进；SQLite 控制表仍可在 offset 不变时原子记录 assembly、execution/model-call 和 Turn outcome。

`database_meta.committed_jsonl_offset` 是每个 session/rollout 的单一权威 committed boundary；`storage_commits.jsonl_offset_after` 是同一边界在提交账本中的不可脱离副本，保留二者不表示存在两个可选择的 offset。对每一条已提交 commit，必须满足 `jsonl_offset_before` 等于该 SQLite 事务开始时的 `database_meta.committed_jsonl_offset`，`jsonl_offset_after >= jsonl_offset_before`，且事务成功后 `database_meta.committed_jsonl_offset == jsonl_offset_after`；下一条 commit 的 `jsonl_offset_before` 必须等于上一条已提交 commit 的 `jsonl_offset_after`。metadata-only commit 的两者都保持不变。插入 `storage_commits`、更新 `database_meta.committed_jsonl_offset`、item catalog/view/control outcome 必须在同一个 SQLite 事务内完成；JSONL item 则必须在该事务前完成 durability barrier。启动恢复必须同时读取并校验 `database_meta`、`storage_commits` 的链、JSONL 文件大小和 item index：缺失、等值不成立、回退、越界或链断裂都必须停止恢复并报告明确的 commit-boundary conflict，reader 不得自行选择另一个值继续。

第一阶段 `assembly_sealed` 是 sealed-before-dispatch 提交：在 provider dispatch 前写入不可变 plan、所有必要 detail reference、tool snapshot、`plan_hash`/request preimage metadata 和 `dispatch_state=ready`。它以 `assembly_id`/model-call idempotency key 幂等，重复 seal 只能返回原快照或报告内容 hash 冲突，不得创建第二个 assembly。若该提交失败，provider request 不得发出。可选的 `dispatch_started`/transport receipt 只能作为同一 assembly 的控制状态更新，不能修改 sealed plan。

第二阶段 `terminal_convergence` 是 provider 返回、失败、中断、空输出或执行丢失后的终态提交：先为本批 canonical item 完成 JSONL durability barrier，再在一个 SQLite 事务中写入 item catalog/view、assembly terminal outcome、execution/model-call outcome、Turn status、`final_item_id` 和 `storage_commits`。provider 空输出、纯失败、执行丢失等没有 canonical item 的结果使用 `commit_kind=terminal_convergence`、`commit_mode=metadata_only`，offset 保持不变：空输出为 `assembly/model_call=completed_empty`、`Turn.status=completed_empty`、`final_item_id=NULL`；失败/中断/丢失分别记录对应 outcome，不能凭空追加 item。若有 output item，`terminal_convergence` 可以使用 `item_bearing` 并同时完成 item convergence，但仍只有一个可见的 SQLite 收敛事务；不能再为同一终态另写一条 metadata-only commit。

恢复只信任 SQLite 的 committed offset、`storage_commits` 和索引校验，不扫描 JSONL 尾部猜测一次 append 是否成功。JSONL 已写但 SQLite 未提交的记录对所有 reader 不可见，可以被回收或保留为未收敛尾部。已存在 `assembly_sealed` 但没有 terminal convergence 时，恢复必须区分 `dispatch_state=ready` 的 sealed-before-dispatch 与已开始 dispatch 但缺少终态的 `unknown/execution_lost`；前者可以使用相同 assembly/model-call identity 继续或显式终止，后者不得自动重用输出 item 或假设 provider 成功。每种 commit 的幂等键在 `(session_id, commit_kind, subject_id, idempotency_key)` 范围内唯一；重放同一提交只返回已提交结果，payload/outcome 不一致必须报冲突。

`ContextAssemblySnapshot` 是 request plan 的不可变语义快照，不是 canonical item；`context_assemblies` 可以更新独立的生命周期状态，但 snapshot 字段在 sealed 后不可变。它记录 `plan_id`、`assembly_id`、`turn_id`、`execution_id`、`model_call_id`、active view、compiler/provider 版本、按顺序排列的 `ContextRef` 或独立的 `ToolSetRef`（内部仍可称 canonical/request-only/tool-set source role）、prompt contribution refs、tool set snapshot、每个 ref 的 included/omission reason、visibility/loss、plan hash 和最终 request hash；`assembly_item_refs` 采用有界行表而不是把整个请求复制进 checkpoint。只有 `RolloutCheckpointSaver` 可以向业务/编译器提供已提交的 plan/snapshot；业务层不得直接扫描 `RolloutStorage`、`AppendWriter` 或内部 context reader。

上述 snapshot 中的“按顺序排列”正式指不可变的 `selection[{plan_ordinal, ref}]`，而不是列表当前排列或数据库返回顺序；每个 entry 必须保存 selection kind、visibility、protection、availability、included/omission/loss、base/delta role 和 source overlay epoch。只有 `included=true` 才强制保存 source revision、逻辑 content length 以及恰一个 content hash 或 redacted stable digest；included request-only 才强制最终 detail ref，included contribution-backed source 才强制 contribution ordinal。`included=false` 的 optional omission 仍保留 tagged ref、plan ordinal、omission/loss、availability 和可得 identity metadata，正文完整性字段与 detail/contribution binding 可以 null/未分配，已知值必须与 manifest 一致。`refs`/contributions 只是 registry，snapshot 必须保存 selection 的完整副本及其 integrity manifest，重启和所有 projector 都不得重新猜测顺序。

在 provider dispatch 前，composer 必须先把完整 plan、source/hash 和 tool-set snapshot 固化为 `sealed` assembly；assembly 持久化失败时不得发起 provider 请求。provider 返回后，canonical output item 的 JSONL fsync、SQLite item catalog/view 更新、Turn execution/model-call outcome、`final_item_id`/partial outcome 和 assembly terminal outcome 必须进入同一个 SQLite 收敛事务；JSONL 已写但事务未提交的 item 对 reader 不可见。provider 已调用但进程在 outcome 提交前退出时，恢复只能标记 `unknown/execution_lost`，不得假设成功或生成 final item。

实时内存 ledger 可以先创建 assembly 并随 delta 更新，但它不是持久化事实。`ContextPlanDetailStore` 的物理归属固定且不再使用“类似”路径：先由 session catalog/path resolver 找到真实 session node，再由 thread catalog/resolver 找到真实 thread node，detail 在 thread node 内的精确相对路径为 `rollout/context-plan-details/<assembly_id>/<detail_id>`；绝对形态为 `${workspace_abs_path}/.boxteam/sessions/<resolved-session-node>/<resolved-thread-locator>/rollout/context-plan-details/<assembly_id>/<detail_id>`。不得写入 `${BOXTEAM_HOME}`、workspace attachment blob store、工作区根目录、默认工作区或按显示名拼接路径。`detail_id` 是该 assembly 内 target-local 的不可变物理叶名；`detail_ref` 是不暴露物理路径的逻辑 typed reference，规范化为 `{session_id, thread_id, assembly_id, detail_id}`，由两级 resolver 唯一映射到上述路径，调用方不得自行拼接路径。session 位于父会话 `children/`、thread 位于日期/hash shard 时仍必须由稳定 ID 和权威 catalog 定位，不能扫描磁盘吸收绕过索引的目录。

detail record 至少绑定 `session_id`、`thread_id`、`assembly_id`、`detail_id`、`detail_kind`、`content_hash`、`length`、`retention_class`、`expires_at`、`visibility`、`protection` 和 `availability`。`detail_ref` 不是第二个物理 ID，而是对这四个 owner/identity 字段的逻辑引用；SQLite 可以保存该 typed ref 和由 resolver 产生的受校验相对 locator，但不得把显示名或任意调用方路径当作 ref。sealed assembly 只允许引用属于同一 SessionThread、assembly 且 hash/length 匹配的 detail；detail 在 seal 后不可变，替换内容必须创建新的 `detail_id`/assembly。跨 session fork 或跨 thread materialization 必须创建 target-local `detail_id` 和 target-local `detail_ref`，source ref 只进 lineage mapping。SQLite 只保存 `detail_ref`、相对定位、长度、hash、retention、visibility 和 availability，不保存可替代 canonical item 正文的第二副本。

detail store 只保存 request-only prompt、middleware 输入/诊断和必要的渲染重放材料。credential、API key、session token、attachment 原文、访问许可和其它 secret MUST 默认以受保护的 redaction marker、类型、长度和本地稳定 digest 表达，不得写入普通 detail；若未来有明确的加密 secret/attachment 子存储合同，也必须使用独立权限和引用，不能绕过本边界。普通 history、checkpoint restore 和默认扩展查询只能看到安全摘要、reference 和 availability；只有同一 workspace/session 权限下显式授权的 detail capability 才能读取 protected 内容，服务不得把物理路径直接暴露给调用方。

detail 写入失败必须区分 required 与 optional：精确 replay 或 dispatch 所必需的 detail 无法写入、校验或绑定时，`assembly_sealed` 失败且不得发起 provider request；非必需 detail 写入失败时，assembly 可以继续，但必须把 `availability=unavailable` 和原因摘要写入 sealed metadata，不得静默丢失。detail 缺失、hash 不匹配、越权或已过期时，默认历史仍可恢复已提交 canonical item，并返回明确的 `detail-unavailable`/`detail-forbidden`/`detail-integrity-error`；精确 replay 必须失败并报告相应错误，不能用当前 middleware 状态伪造旧 plan。

detail GC 由 session 级 retention policy 控制：只有超过 `expires_at`、不再被 active execution、sealed assembly、checkpoint、operation anchor 或显式 extension pin 引用时才可回收。GC 先以可恢复的 tombstone/availability 更新记录，再删除物理正文；不得删除 canonical JSONL、item catalog、Turn、assembly sealed metadata 或 provenance reference。实时内存 ledger 可以先创建 assembly 并随 delta 更新，但只在上述 session detail store 和 SQLite reference 持久化后才可对外承诺可重放。

`plan_hash` 和 `request_hash` 使用带算法标识的 `sha256:jcs:v1`：输入先按 RFC 8785 JSON Canonicalization Scheme 生成无空白 UTF-8 JSON，对象 key 递归字典序排序，数值按 JCS 规范规范化，具有语义的数组（item/ref/content-part/tool 顺序）保留其 ordinal 顺序；集合型字段必须先按稳定 identity 排序。缺省字段统一省略，只有协议明确表示语义 null 时才编码 null，不能由 serializer 的默认值决定 hash。

`plan_hash` 只覆盖 provider-neutral 的 `ContextRequestPlan`，其 v2 preimage schema 固定为 `context-plan-hash:v2`。除 format/schema version、active view identity/revision、按 `plan_ordinal` 排列的 canonical/request-only/overlay selection、每个 ContextRef 的 source/version/hash/length、贡献顺序和 selection/visibility policy 外，preimage MUST 显式包含从 `ContextRequestPlan.tool_set_refs[]` 选出的 `tool_set_refs[]` manifest identity。每个 ToolSetRef 条目必须包含 `tool_set_snapshot_id`（即 `ref_id`）、`source_revision`、`content_length`、恰一个 `content_hash` 或 `redacted_stable_digest`、`tool_set_schema`、`tool_set_schema_version`、`tool_policy_version` 以及 manifest 绑定的规范 `tool_policy`；这些字段必须直接取自同一 ToolSetRef/`tool_set_manifest[]`，不得以独立计算的笼统 logical tool contract hash 代替。为消除 registry 容器顺序差异，preimage 中 `tool_set_refs[]` 按 `tool_set_snapshot_id` 排序，而 `selection[]` 仍按 `plan_ordinal` 保留语义顺序；每个 ToolSetSnapshot 内的 tools 按稳定 `tool_id` 排序。未被 selection 选中的 registry entry 不影响本 plan hash，新增、删除或替换被选中的 ToolSetRef 必须改变 preimage。canonical item ref 缺少 content hash 时 plan 不可 seal。它排除 `assembly_id`、`execution_id`、`model_call_id`、创建时间、transport header、provider request ID、wire role、provider/model capability 和 provider-specific loss；因此同一 committed plan、同一 selection 与同一 ToolSetRef manifest 被不同 provider projector 使用时必须得到同一 `plan_hash`。`request_hash` 的规范化 wire request 也必须由其 canonical item refs/content hashes、request-only contribution hashes 以及同一 ToolSetRef manifest 间接绑定，不能只 hash 一个脱离来源的拼接字符串。

`request_hash` 覆盖某个具体 projector 的规范化请求 preimage：`projector_id`/version、provider family/model、wire role、规范化后的请求 content、绑定的 ToolSetRef manifest/tool schema/config、附件引用、redaction/loss markers 和目标能力。它排除 provider request ID、retry/attempt identity、时间戳、认证信息、网络 header 和 transport tracing。敏感字段不得把原文放入 hash preimage，必须替换为 `redaction_class`、长度和 workspace/session 作用域内的稳定 digest；detail_ref 只作为可选 reference/availability，不把 detail store 正文复制进 preimage。相同 `plan_hash` 是跨 provider 的可比基线；相同 ToolSetRef manifest 允许不同 provider 得到不同 `request_hash`，因为 wire tool schema/config 编码可以不同；`request_hash` 只在相同 `projector_id`/version 和 provider/model profile 内用于 exact replay，跨 provider 不要求相等。

assembly 必须持久化 `hash_algorithm`、`plan_hash`、`request_hash` 及其 preimage schema/version。seal、restore 和 replay 必须先逐字段校验 `tool_set_refs[]` 与对应 `ToolSetRef`/`tool_set_manifest[]`，再比较 `plan_hash`；ToolSetRef 的 snapshot id、source revision、length、hash token、schema/policy version 或 manifest 内容发生变化时，必须创建新的 ToolSetRef/plan，并报告 `plan-hash-mismatch`，不能从当前 tool registry、空 tools 或旧 logical tool contract hash 静默复用旧 plan。manifest 无法解析、不可用或字段不一致时，即使调用方带有旧 `plan_hash` 也必须报告 `source-mismatch`/`detail-unavailable` 并禁止 dispatch。`plan_hash` 相同但同一 projector 的 `request_hash` 不同，必须报告 request mismatch 并禁止宣称 exact replay；不同 provider 只要 ToolSetRef manifest 相同就必须保留共同的 `plan_hash`，provider-specific request hash 不同不构成 plan mismatch，但必须保留显式 loss report。provider request ID 可以作为 outcome metadata 保存，但永远不参与上述两个 hash。

### 2.1 RFC 8785 是不可替代的 canonical serializer

`sha256:jcs:v1` 的实现合同不是“JSON 对象排序后序列化”，而是 RFC 8785 JSON Canonicalization Scheme 的完整结果。实现 MUST 使用经过 RFC 8785 一致性验证的 serializer：对象属性按 UTF-16 code-unit lexicographic order 排序，数组保持 schema 定义的语义顺序，数字遵循 JCS/ECMAScript 可表示有限 IEEE-754 数字的序列化规则（包括 `-0`、指数和最短表示），字符串按 JSON/JCS Unicode 规则转义，禁止 NaN、Infinity 和其它非法 JSON number；输出必须是无空白 UTF-8 bytes。`json.dumps(sort_keys=True)`、语言默认 map 排序或仅按 Unicode code point 排序都不是替代实现。

`content_hash` 的 preimage 固定为 `{ "payload_kind": <payload_kind>, "payload": <payload> }` 的 JCS bytes；`plan_hash`、`request_hash`、acceptance idempotency key 和 legacy seed 必须引用同一版本化 serializer，并把其 preimage schema/version 写入 assembly/migration metadata。具有语义的 ref/contribution/content-part/tool 数组按已分配 ordinal 保序，真正的集合字段才按稳定 identity 排序；不能由 serializer 的默认排序改变业务顺序。实现验收必须以至少一组跨语言 golden vector 覆盖浮点、`-0`、指数、Unicode/UTF-16 key、数组顺序和非法数字，证明 content/hash/idempotency 在重启与跨语言恢复中相等或明确拒绝。

### 4. 流式 item 先 draft，终态才进入 canonical history，并携带实时 provenance

`MessageStreamRuntime` 继续负责 `block.started`、`block.delta`、`block.completed`、tool-call delta 和 stream 终态；它可以为订阅者提供低延迟状态，也可以维护恢复 snapshot，但不能把网络 chunk 当成 rollout item。

每个 model call 的 normalized delta 进入带 `item_id`、`assembly_id` 和 provenance 的 `ItemDraft` accumulator。收到正常完成、用户中断、Provider failure 或 execution lost 后，由同一个 turn writer 将 draft finalization 为 `completed`、`partial`、`failed` 或 `unknown` item。middleware 产生的 prompt contribution、view transform、tool policy 和内部状态更新也通过同一个 `ContextAssemblyLedger` 记录来源与版本，但不会被伪装成 provider 输出 item。

已经提交给前端的 block delta 必须先具备可恢复的 message-stream 语义和来源引用；如果 canonical item 尚未终态化，重连依赖 message-stream snapshot/assembly metadata，而不是读取一个伪造的完整 LangChain message。内存 ledger 丢失时只能恢复已经落盘的 item、assembly snapshot 和明确的 execution-lost 状态，不能根据最终 AIMessage 猜回缺失的 middleware provenance。

这保证实时显示和历史事实都来自同一份 normalized identity/order，同时允许将多个 chunk 合并为一个语义 item，并保留中断时的明确 partial 事实。

### 5. Context middleware 只返回 contribution，最终 adapter 才构造 LangChain request

Context compiler 不接收“最后一条 `list[BaseMessage]`”作为输入，而是接收 active context view、请求用途、visibility policy、工具能力、附件许可、Provider capability 和 runtime context source，输出固定的 `ContextRequestPlan`。但这些输入只能由 `RolloutCheckpointSaver` 通过已提交的 context view、已提交 item/source reference 和已提交或正在打开的 assembly snapshot 入口提供；业务 service、middleware adapter、LangChain projector 和 Provider projector 不得直接扫描 `RolloutStorage`、`AppendWriter` 或内部 context reader，也不得自行拼接 JSONL/SQLite。

`RolloutCheckpointSaver` 是 plan/snapshot 的 owner boundary：它负责验证 view lineage、读取 committed item ranges、合并 request-only contribution、分配 plan identity、seal/读取 `ContextAssemblySnapshot` 并将不可变的 `ContextRequestPlan` 交给 projector。projector 只能消费 Saver 返回的 plan/snapshot 和显式的 runtime contribution input；不能通过旁路读取存储重新计算已 seal 的 plan。未 seal 的内存 ledger 只属于 Saver 内部，业务层不得把它当作可恢复或可重放的事实。

middleware 不得直接修改 canonical item，也不得将修改后的 `ModelRequest.messages`、`system_message` 或 `tools` 作为新的历史状态写回。

### 5.1 缓存保持型 source overlay

现有运行时对 workspace `AGENTS.md` 的行为应被提升为统一的结构化 overlay 合同：首次进入某次已 seal request 的完整 source revision 是稳定 `base_ref`；source 后续发生变化时，不替换这个 base，也不把 diff 当作普通 `HumanMessage` 追加，而是创建带 `from_revision`、`to_revision`、diff/hash 和 source reference 的有序 delta contribution。需要跨 checkpoint 继续生效的 delta 通过明确的 `PersistItemIntent` 追加为 `semantic_kind=runtime_notice`、`turn_scope=ambient`、`payload_kind=structured_content` 的 canonical item；仅对下一次请求有效的 patch 保持 request-only。两者都不创建 Turn root/member，wire role 只是 projector 的兼容编码。

一个 source 在未触发物化前可以形成 `A(base) -> B(delta) -> C(delta)` 链。每个 delta 的基线必须是前一个已观察 revision，使用稳定的 source revision/hash、diff algorithm/version 和 `(source_ref, from_revision, to_revision, diff_hash)` 幂等键；delta 通过 `supersedes`/`materializes` provenance relation 连接，不能原地修改旧 base 或旧 delta。`ContextRequestPlan` 保留 `source_overlay_epoch`、`base_ref`、有序 `delta_refs` 和当前 target revision，wire projector 先使用稳定 base，再按确定顺序应用 delta，从而保持 provider 可缓存的前缀。

overlay 的 `base_ref` 与每个 `delta_ref` 在 `included=true` 时必须是带完整 integrity manifest 的 `ContextContribution`/`ContextSelectionEntry`：至少保存 `source_revision`、`content_length`、`content_hash` 或 `redacted_stable_digest`、`source_overlay_epoch`、`base_delta_role`、`from_revision`/`to_revision`（delta）、`diff_hash`（delta）以及其 assembly-bound `contribution_ordinal` 和 selection `plan_ordinal`。optional overlay omission 的 entry 仍保留 tagged ref、plan ordinal、omission/loss、availability 和可得 source identity，但 detail、正文 length/hash 与 contribution ordinal 可以 null/未分配，且不得被应用为 delta。projector 不得只凭 ref 字符串重新读取当前 source；included entry 任一字段缺失、不匹配或 ordinal 冲突都返回 `source-mismatch`/`detail-unavailable`/`plan-order-integrity`，omitted entry 只恢复 omission/loss metadata。

`history_view_revision` 与 `source_overlay_epoch` 必须分开。rewind、replay、同 session branch/view 切换、history prefix fork 和 compaction 首先只产生新的 history view/revision，并通过 `ContextReconciliation` 在下一次 assembly 中重新选择历史 item；它们不因为历史尾部变化就推进 source overlay epoch。若旧 base/delta 仍兼容，reconciliation 复用原 overlay；即使 delta item 随历史尾部被新 view 隐藏，也必须从独立的 ambient overlay lineage 重新注入。只有 source base/detail 不可恢复、overlay 链需要压缩、source 删除/权限或 policy boundary 改变、Provider/projector 缓存前缀确实不兼容、或显式 source refresh 时，才推进新的 `source_overlay_epoch` 并物化完整 source revision。

每次会改变运行时上下文的操作都先比较两类状态：`history_view_revision` 描述 canonical history 的选择，`source_overlay_epoch` 描述 source base/delta 的版本。`ContextReconciliation` 至少记录 `from_view_revision`、`to_view_revision`、`previous_overlay_epoch`、`next_overlay_epoch`、source revision set、base/delta refs、reason 和 outcome；outcome 固定为 `history_view_changed`、`overlay_reused`、`delta_appended`、`overlay_materialized` 或 `overlay_invalid`。只有 `overlay_materialized` 才改变 source base；`history_view_changed` 可以与 `overlay_reused` 同时出现。

例如 `AGENTS=A(base) + A→B(delta)` 后 rewind 到 A 之前的历史边界，新 assembly 仍可使用旧 A base 并重新注入 A→B delta；canonical history 保持 rewind 后的旧 view，overlay 不因物理位置或 view range 被删除。若目标 view 没有可恢复的 source base，才以当前 B 建立新 source overlay epoch；不能把“历史 view 变了”本身当作 source materialization 理由。

`ContextAssemblySnapshot` 必须分别记录 `history_view_revision` 和 overlay lineage：`source_overlay_epoch`、base source revision/reference、按序 delta references、target/materialized revision、materialization reason 和 overlay hash。assembly seal 后这些选择不可变；seal 之后观察到的 source edit 或 history view transition 只影响下一次 assembly。source 读取失败、权限变化、revision/hash mismatch 或 detail 缺失时，系统必须返回明确的 source-mismatch/detail-unavailable 状态，不能静默读取当前文件拼出旧 request。

`AGENTS.md` 的完整正文和 diff、skill frontmatter metadata 与按需加载的 `SKILL.md` 正文都使用这套 source-revision 模型。skill 未被当前 request 使用时，不因为文件变化而额外注入 delta；已进入 request 的 skill metadata/body 才建立对应 base/delta lineage。现有 `_workspace_agents_snapshot` 与位置型 `_summarization_event` 只能由兼容 adapter 转换为 source/view reference，新的 canonical 设计不再以 raw message snapshot 或 `cutoff_index` 表示 overlay。

middleware 的统一输出类型为 `ContextContribution`，按用途分为：

- `PromptContribution`：请求级 system/developer/context segment，带 source、version/hash、scope、order、visibility 和可选 supersedes reference；若 source 已进入缓存保持型 overlay，还必须带 source revision、source overlay epoch、base/delta 角色和 diff/materialization reference。静态 system prompt、skill 说明、workspace/environment snapshot、动态 memory 等默认属于此类 request-only contribution；只有通过 `PersistItemIntent` 明确提升，才成为 canonical `runtime_notice` 或其它明确语义的 item；
- `ToolSetSnapshot`：本次请求可见工具、schema、execution policy、provider capability 和 snapshot hash；
- `ContextViewTransform`：选择/过滤/压缩 item references，不修改 JSONL 正文；
- `PersistItemIntent`：需要成为后续上下文事实时，请求 writer 追加新的 canonical item；
- `DetailReference`：指向受保护的 middleware 输入、诊断或扩展 metadata，不直接暴露正文。

`ContextRef` 只允许以下两种生命周期明确的引用；在 ContextRef/selection 中，`request_only` 只能出现在第二种引用的 `ref_type` 中，不是 `CanonicalItemRecord` 的字段、语义枚举或 payload 类型。`ContextContribution` 的同名 boolean 是独立的、恒为 true 的贡献本体不变量，见下文，不是 ContextRef discriminator：

```text
ContextRef
├── ref_type=canonical_item
│   ├── session_id             # owner namespace
│   ├── ref_id=item_id
│   ├── item_sequence
│   ├── semantic_kind
│   ├── payload_kind
│   ├── status
│   ├── source_revision       # committed item_catalog source revision
│   ├── content_length        # logical payload bytes, not JSONL line length
│   └── content_hash          # from immutable CanonicalItemRecord
└── ref_type=request_only
    ├── session_id             # owner namespace
    ├── ref_id=plan_item_id
    ├── plan_id                 # required draft/plan scope
    ├── source_ref/detail_ref
    ├── source_revision?       # required for included source; known identity if available
    ├── content_length?        # required for included source
    ├── content_hash?          # included selection exactly one; draft/omitted may be absent
    ├── redacted_stable_digest? # included selection exactly one; draft/omitted may be absent
    └── protection/availability
```

`ref_type` 是 ContextRef 在 plan、snapshot、SQLite assembly ref 和 projector 输入中的唯一序列化判别字段，闭合集合只有 `canonical_item | request_only`；`ref_kind` 和 `request_only` boolean 作为 ContextRef/selection 的并列 discriminator 都禁止持久化或进入 wire，`item_id`/`plan_item_id` 也不能替代 `ref_type`。`ref_id` 是统一身份字段：canonical ref 的 `ref_id` 等于 immutable item 的 `item_id`，request-only ref 的 `ref_id` 等于 plan 内 target-local 的 `plan_item_id`。所有 ref 都显式带 `session_id`；canonical ref 的解析范围是 `(session_id, item_id)`，request-only draft ref 的解析范围是 `(session_id, plan_id, ref_id)`。assembly 不是 ContextRef 的 identity scope，也不是 draft ref 的必填字段：assembly 绑定只由 sealed `ContextSelectionEntry.assembly_id` 及其 `ref_manifest`/detail manifest 表达。实现内部如需接收旧 `ref_kind`/boolean，只能在 legacy ingress adapter 立即归一化为 `ref_type`/`ref_id`，不得让两套判定进入 composer、storage 或 projector。canonical ref 必须能唯一解析到同一 session 已提交的 `item_catalog`；request-only ref 在 unsealed plan 中只解析到同一 `(session_id, plan_id)` registry，只有 seal 后才允许由 selection/manifest 解析到该 assembly；一个 ref 不能同时属于两种类型。这里的“必须解析”仅适用于 `included=true` 的完整 ContextRef；optional `included=false` entry 的 tag/id identity stub 不声称已解析到 item/detail，也不触发该正文解析要求。

对于 `included=true`，`ContextRef` 必须解析为完整 source manifest，并满足对应的 revision、logical content length 与恰一个 hash token；对于 optional `included=false`，selection 中的 `ref` 仍保留唯一的 tag/id（canonical 或 request-only，tool_set 则为 ToolSetRef tag/id），但它可以是尚未解析完整 manifest 的 typed identity stub。该 stub 的已知 source identity 若存在必须与 registry/manifest 一致，缺失的 detail、正文 length/hash 不得被实现补造或解释为空正文；omitted entry 不进入 source dereference 或 dispatch。

`ContextRef` 的上述字段是完整的可恢复引用合同，不能只传裸 `item_id` 或 `plan_item_id`。`content_length` 是按 payload schema 编码后的正文长度：文本为 UTF-8 bytes，structured/summary/tool payload 为其确定性 JCS UTF-8 bytes，opaque/attachment 为 schema 指定的 encoded value bytes；它不是 JSONL line `offset/length`。来源必须按 selection union 分流：`ref_type=canonical_item` 的 canonical ref 的 source revision 与 content length 必须来自同一 session 已提交 `item_catalog.payload_length`（没有外部 source revision 时由 writer 固定为 `canonical:<item_id>:<content_hash>`），`ref_type=request_only` 的 sealed selection 必须来自同一 assembly 的 detail/contribution source manifest，`ToolSetRef.ref_type=tool_set` 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest；三者都不能使用 JSONL line 长度、offset 或 wire message 长度替代，content hash 也必须按各自 immutable/manifest source 在 included 读取时复核。request-only draft ref 只需携带当时可得的 source/detail revision、content length 和 hash token；正文尚未解析、optional source 不可用或敏感 metadata 不可暴露时这些字段可为 null/未分配，若保留 hash token 则 content_hash 与 redacted_stable_digest 恰好一个存在且必须与 source manifest 一致。draft ref 的 `detail_ref` 不得是可拼接的物理路径，也不得含未分配的 assembly identity：若正文暂存在内存 ledger，draft 中可为 null；若来自已存在的 source detail，只能带 typed `source_ref`，由 seal preflight 读取并校验。Saver 只有在成功分配 assembly 后，才将 included request-only 正文写入该 assembly 的 detail store，并在 sealed selection/manifest 写入最终 `{session_id, assembly_id, detail_id}` `detail_ref`；sealed detail_ref 的解析发生在 selection/manifest 校验和 projector/restore 读取时。ref 的 metadata/hash/length 不能替代缺失正文：required source 无法按这些字段取回并校验时必须返回 `source-mismatch` 或 `detail-unavailable`；optional omitted source 不得被恢复为正文。

工具集合采用独立的 `ToolSetRef`，不是 `ContextRef` 的第三种 `ref_type`：`ContextRef.ref_type` 仍严格只有 `canonical_item | request_only`，`ToolSetRef.ref_type=tool_set` 只在 selection 的 tagged-union `ref` 中合法。`ToolSetSnapshot` registry entry 归属于 `(session_id, plan_id)`，其 target-local `tool_set_snapshot_id` 同时作为 `ToolSetRef.ref_id`，并受 `UNIQUE(session_id, plan_id, tool_set_snapshot_id)` 约束；unsealed plan 中 `assembly_id=NULL`，Saver seal 后在同一 assembly binding 中填入 `assembly_id`，新 plan 必须使用新的 ref identity，即使底层 schema/hash 相同也不能跨 plan 借用 registry identity。`ToolSetRef` 必须同时携带 `tool_set_schema`、`tool_set_schema_version`、`tool_policy_version`；其 `source_revision` 来自工具 registry/config revision，`tool_policy` 的规范内容只通过同一 ToolSetSnapshot manifest 绑定。`content_length` 是如下 manifest preimage 的 RFC 8785 JCS UTF-8 bytes 长度，`content_hash` 是该 bytes 的 `sha256:jcs:v1` 小写 hex，受保护 manifest 则以恰一个 session-scoped `redacted_stable_digest` 替代并在 protected registry 保留内部 hash：`{ "tool_set_schema": "tool-set-ref", "tool_set_schema_version": "v1", "tools": <按稳定 tool_id 排序的规范 schema/config entries>, "tool_policy": <规范 policy>, "tool_policy_version": "v1" }`。`protection` 固定为 `public | redacted | protected`，`availability` 固定为 `available | unavailable | forbidden | expired`；缺失正文、不可读 registry 或 hash/length/source mismatch 分别返回 `detail-unavailable`/`source-mismatch`，不得以空 tools 继续。上述完整 manifest 字段适用于可解析的 ToolSetRef registry/included selection；optional omitted tool-set entry 只需保留 `ref_type=tool_set`/`ref_id` tag/id、plan ordinal、omission/loss/availability 和可得 identity，不声称 manifest 已可用，也不得投影 tools。

```text
ContextContribution
├── contribution_id
├── contribution_kind       # prompt | overlay_base | overlay_delta | notice
├── request_only             # required true; contribution-intrinsic, not a ref discriminator
├── body/detail_ref
├── source_revision         # required, immutable source revision
├── content_length          # logical body bytes, same definition as ContextRef
├── content_hash?           # ordinary body
├── redacted_stable_digest? # protected/sensitive body; exactly one hash token
├── protection/visibility
└── ordinal_binding?
    ├── assembly_id
    └── contribution_ordinal

ToolSetRef
├── ref_type=tool_set        # ToolSetRef discriminator; not ContextRef.ref_type
├── ref_id=tool_set_snapshot_id
├── plan_id                  # required; plan-scoped registry identity
├── assembly_id?             # NULL in unsealed registry; required in sealed selection
├── source_revision          # tool registry/config revision
├── tool_set_schema          # namespaced manifest schema identifier
├── tool_set_schema_version  # explicit manifest schema version
├── tool_policy_version      # explicit policy version in manifest
├── content_length           # JCS UTF-8 bytes of the tool-set manifest
├── content_hash?            # ordinary manifest
├── redacted_stable_digest?  # protected manifest; exactly one hash token
├── protection               # public | redacted | protected
└── availability             # available | unavailable | forbidden | expired

ContextSelectionEntry
├── assembly_id              # required; entry cannot exist outside an assembly
├── plan_ordinal             # unique within assembly_id, selection authority
├── ref                      # exactly one tagged union: ContextRef | ToolSetRef
├── selection_kind           # canonical_history | request_only | overlay_base |
│                            # overlay_delta | tool_set
├── included                 # true or false
├── omission_reason?         # required when included=false
├── loss[]                   # explicit capability/source loss markers
├── visibility
├── protection
├── availability
├── source_revision?         # required iff included=true; known iff manifest agrees
├── content_length?          # logical body length; required iff included=true
├── content_hash? / redacted_stable_digest? # included=true exactly one; included=false optional at most one
├── detail_ref?              # required iff included=true and ref_type=request_only
├── contribution_id?         # required iff included=true and contribution-backed; forbidden canonical/tool_set
├── contribution_ordinal?    # required iff included=true and contribution-backed
├── base_delta_role          # none | base | delta
└── source_overlay_epoch?

ContextRequestPlan
├── plan_id                  # created before seal, unique/idempotent in session
├── plan_state                # unsealed | sealed
├── assembly_id?              # NULL until Saver successfully seals
├── history_view_revision
├── source_overlay_epoch
├── refs[]                   # ContextRef registry, not an ordering authority
├── tool_set_refs[]          # ToolSetRef registry, not an item/contribution registry
├── contributions[]          # source registry, not an ordering authority
└── selection[]              # sole ordered selection consumed by projectors

ContextAssemblySnapshot
├── plan_id                  # source plan identity, retained after seal
├── assembly_id               # Saver-assigned sealed/dispatch identity
├── sealed_plan               # immutable ContextRequestPlan identity/hash
├── selection[]              # exact persisted copy of plan.selection
├── ref_manifest[]           # exact ContextRef source manifest
├── tool_set_manifest[]      # exact ToolSetRef source manifest
├── contribution_manifest[]  # exact body and ordinal binding manifest
└── plan_hash/request_hash/loss
```

### Selection kind 与 ref type 的唯一兼容矩阵

下表是 `ContextSelectionEntry.selection_kind` 与 tagged-union `ref` 的唯一规范矩阵；`included=true` 与 `included=false` 都必须先满足同一行的 tag/type 兼容性，omitted entry 只是未解析 source body 的 identity stub，不得借此改变历史/请求生命周期。任何不在表内的组合必须在 source dereference 前返回 `plan-order-integrity`，不得按 payload、wire role 或当前 registry 猜测另一种类型。

| `selection_kind` | 唯一合法 `ref` | `included=true` 绑定与字段 | `included=false` optional omission | 投影/恢复行为 |
|---|---|---|---|---|
| `canonical_history` | `ContextRef.ref_type=canonical_item` | 只能使用同一 session 的 `item_catalog` item；source revision、logical content length、恰一个 hash token 必填；`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 必须为 NULL，`base_delta_role=none` | 仍只能保留 `canonical_item` tag/id、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 item identity；正文完整性字段可空/未分配 | history/restore 保留 omission metadata，跳过 canonical message；不得读取当前 item 或生成空 message |
| `request_only` | `ContextRef.ref_type=request_only` | `detail_ref` 必须是同 assembly 的 sealed detail；若 contribution-backed，`contribution_id` 与 `contribution_ordinal` 必填并唯一指向同一 plan 的 contribution manifest；`base_delta_role=none`、`source_overlay_epoch` 为 NULL | 仍只能保留 `request_only` tag/id、plan ordinal、omission/loss/availability 和可得 identity；detail/ref、正文完整性字段与 contribution binding 可空/未分配 | projectors/restore 跳过 omitted detail/body，不回退当前 middleware/source；included body 按 contribution_id（如有）读取 |
| `overlay_base` | `ContextRef.ref_type=request_only` | 必须是 contribution-backed；`contribution_id`、`contribution_ordinal`、source revision、logical length、恰一个 hash token、`detail_ref` 必填；`base_delta_role=base`、`source_overlay_epoch` 必填，并绑定完整 base source | 保留 `request_only` tag/id、`plan_ordinal`、`base_delta_role=base`（及可得 epoch/identity）、omission/loss/availability；正文/detail/hash/contribution binding 可空/未分配 | 不应用 omitted base；restore/projector 保留 loss，不以当前 source 替代 |
| `overlay_delta` | `ContextRef.ref_type=request_only` | 必须是 contribution-backed；同样强制 `contribution_id`、`contribution_ordinal`、detail、source revision/length/hash、`base_delta_role=delta`、`source_overlay_epoch`，并校验 `from_revision`/`to_revision`、diff algorithm/version、`diff_hash` 与 chain | 保留 `request_only` tag/id、`plan_ordinal`、`base_delta_role=delta`（及可得 epoch/identity）、omission/loss/availability；正文/detail/hash/contribution binding 与 diff metadata 可空/未分配 | 不应用 omitted delta，不推进或重建 overlay；history-only restore 只暴露 omission/loss |
| `tool_set` | `ToolSetRef.ref_type=tool_set` | 只能使用同一 plan/assembly 的 ToolSetSnapshot manifest；manifest source/length/hash/schema/policy 字段必填；`base_delta_role=none`、`contribution_id`、`contribution_ordinal`、`detail_ref`、`source_overlay_epoch` 必须为 NULL | 仍只能保留 `tool_set` tag/id、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 registry identity；manifest 正文字段可空/未分配 | history/restore 不生成工具定义；Provider 不投影该 tool set、不回退当前 registry 或空 tools |

矩阵中的 `contribution_id` 是 included 且 contribution-backed 的 request-only/overlay entry 到 `contribution_manifest` 的唯一映射，不能由 `ContextRef.ref_id`、`detail_ref`、hash 或 ordinal 推导；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref` 与 source manifest 解析，不要求 `contribution_id`/`contribution_ordinal`；`overlay_base|overlay_delta` 按矩阵始终必须 contribution-backed。`ContextRef.ref_id` 仍是 canonical `item_id` 或 request-only `plan_item_id`。included 且 contribution-backed 的 request-only/overlay 正文、source revision、length/hash、detail 和 contribution ordinal 必须从同一 `contribution_id` manifest 逐字段校验。omitted entry 必须保留表中规定的 tag/type，即使 source 不可解析也不能改成另一类 ref；只要 source 被标记 required，任何 omission/detail failure 都必须拒绝 seal/dispatch。

`ContextContribution` 的固有语义是 request-only：其持久化字段 `request_only` 必须存在且恒为 `true`，用于贡献 manifest 的不变量校验，不用于判别 `ContextRef` 的 union 类型；`false`、缺失或把该字段复制到 ContextRef/selection discriminator 都是 schema error。`ContextContribution.contribution_kind` 的闭合集合只有 `prompt | overlay_base | overlay_delta | notice`。`tool_set` 不是合法的 contribution kind：Provider tool definitions 只能由同一 plan/assembly 的 `ToolSetSnapshot` registry 和 `ToolSetRef` 提供，不能由 request-only contribution、普通 contribution body 或 `contribution_ordinal` 表达。v2 schema 遇到 `contribution_kind=tool_set` 必须返回 `contribution-kind-unsupported`；只有一次性 `legacy_import_v1_to_v2` migration reader 可以把原始记录保留在 migration report/quarantine（例如 `legacy_tool_set_contribution`）供人工审计，但不得把它转换为 ContextContribution、ToolSetRef 或 Provider tools，也不得被正常 runtime 调用。只有显式可解析的 ToolSetSnapshot manifest 才能创建 ToolSetRef。

`ContextContribution.content_hash` 的正文 preimage 固定为 `{ "contribution_kind": <contribution_kind>, "body": <typed body> }` 的 `sha256:jcs:v1` RFC 8785 JCS bytes；`body` 为 inline typed value 或从 `detail_ref` 取回的完整 typed body，不包含 `contribution_id`、ordinal、assembly、时间或 wire role。敏感/受保护正文若不允许暴露该 hash，ref 中 `content_hash` 必须为空，改带恰一个 `redacted_stable_digest= hmac-sha256:session:v1:<64位小写hex>`；protected detail manifest 仍保存并校验内部 `content_hash`，普通 reader 只见 stable digest。canonical `ContextRef` 始终使用 item 的 `content_hash`，request-only ref/selection 使用 contribution/detail 的 hash token，不能把不同 preimage 混称为同一个正文长度或 hash。

`ContextRequestPlan` 的 lifecycle 与 identity 固定为：`create_context_plan` 先生成 session 内唯一的 `plan_id` 和 `plan_state=unsealed`；计划可以暂存 `refs[]`、`tool_set_refs[]`/`contributions[]` registry，但在未分配 assembly 前 `assembly_id=NULL` 且 `selection=[]`，因此不存在任何 `ContextSelectionEntry` 或 `plan_ordinal`，也不得 dispatch。plan registry 中的 ContextRef 以 `(session_id, plan_id, ref_type, ref_id)` 唯一；canonical ref 可在不同 plan 被复用，request-only ref 不得跨 plan 复用。plan creation 的幂等键在 `(session_id, plan_creation_idempotency_key)` 内唯一，同 key 的初始输入不一致返回 `plan-idempotency-conflict`。Saver 只有在 seal preflight 已验证全部 ContextRef/ToolSetRef/contribution registry source、detail、hash/length 和 selection consistency 后，才在一个提交事务中分配新的 session-local `assembly_id`，将 `plan_state` 变为 `sealed`，生成完整 `selection[]`、`plan_ordinal` 和 assembly-bound `contribution_ordinal`，并写入 `ContextAssemblySnapshot`；seal 期间将 request-only body 解析/物化为最终 detail_ref，失败不得产生可 dispatch 的 assembly/selection，原 unsealed plan 只能修正后重试。`seal_context_assembly` 的 `(session_id, plan_id, seal_idempotency_key)` 对相同 preimage 返回同一 `assembly_id`/snapshot，对不同 preimage 返回 `assembly-idempotency-conflict`，不得复制 assembly。

`plan_state` 的合法转移只有 `unsealed -> sealed`；seal preflight 失败记录独立的 seal failure/control outcome，plan 仍是可修正、可重试的 `unsealed`，不生成 assembly 或 selection entry；显式放弃的 plan 由外部 control record 标记，不伪造 `sealed`。seal 成功后 `ContextRequestPlan` 的 `plan_id`、`assembly_id`、registry、selection 和 hash 全部不可变；snapshot 保存它们的逐字段副本，二者是一对一的 sealed semantic identity，不允许把 plan_id 当成 assembly_id 或把 pre-seal plan 当成 sealed assembly。`assembly_id` 是 dispatch、assembly refs、ordinal binding 和 terminal outcome 的作用域；`plan_id` 是 draft/plan 幂等和审计作用域。需要不同 source/view/selection 必须创建新的 `plan_id` 和新的 assembly，旧 snapshot 不更新。没有任何 source 的合法 sealed plan 可以保存空 selection，但该空 selection 仍必须属于已分配的 assembly scope。

`ContextContribution.contribution_ordinal` 是 assembly-bound binding，不是跨 assembly 的 contribution 本体全局 ordinal：composer 为某个 `assembly_id` 选入 contribution 时分配它；同一逻辑 contribution 被另一 assembly 复用时获得该 assembly 的新 ordinal，但同一 assembly 内不可重写。该 binding 与正文、source revision/hash/length 一起写入 `assembly_item_refs`（或同等固定的 assembly ref catalog），并受 `UNIQUE(assembly_id, contribution_ordinal)` 与 `UNIQUE(assembly_id, contribution_id)` 约束；内存 ledger 丢失后只能从这些已提交行恢复。

`ContextSelectionEntry.contribution_id` 是 contribution-backed source 的唯一显式映射键：当 `included=true` 且 entry 的 source 由 `ContextContribution` 提供时，entry 必须同时保存非空 `contribution_id` 和 assembly-bound `contribution_ordinal`；该 ID 必须在同一 `(session_id, plan_id)` 的 `contribution_manifest` 中唯一解析，并由 manifest 明确绑定该 entry 的 `ref_type`、`ref_id`、`detail_ref`/body locator、source revision、logical content length 和 hash token。`ContextRef.ref_id` 仍保持其既定语义（canonical 为 `item_id`，request-only 为 `plan_item_id`），不得通过 ref_id、detail_ref、hash 或 ordinal 猜测 contribution；restore 和三种 projector 必须先按 `contribution_id` 取 manifest/body，再按 entry 的 integrity 字段复核。`assembly_item_refs.contribution_id` 与 `contribution_ordinal` 均可为 NULL；普通非 contribution-backed 的 included request-only 不分配 contribution binding；included 且 contribution-backed 的 request-only/overlay 必须保存非空两字段并校验同一 manifest。optional omitted entry 不新分配 `contribution_id` 或 `contribution_ordinal`，仅 omitted request-only/overlay 可保留已有且与同一 manifest 一致的 `contribution_id`，且不得读取正文/detail，没有既有映射则为 NULL；canonical entry 和 ToolSetRef entry（included 或 omitted）的 `contribution_id`、`contribution_ordinal`、`detail_ref` 始终为 NULL，不能借用 contribution manifest 表达其正文或工具定义。

`ContextRequestPlan.refs`、`.tool_set_refs` 与 `.contributions` 只提供可查的 source registry，`selection` 才是唯一顺序和 inclusion/loss authority。每个 selection entry 必须恰好解析到一个 tagged-union source ref：`canonical_history|request_only|overlay_base|overlay_delta` 解析到一个 `ContextRef`，`tool_set` 解析到一个 `ToolSetRef`；`plan_ordinal` 在同一 `(session_id, assembly_id)` 内唯一且按 selection 顺序递增。同一 ref/contribution/tool-set snapshot 不得重复选择：ContextRef 受 `UNIQUE(session_id, assembly_id, ref_type, ref_id)`、ToolSetRef 受其 tagged identity 的 assembly-local 唯一约束，included contribution 受 `UNIQUE(assembly_id, contribution_id)` 与 `UNIQUE(assembly_id, contribution_ordinal)` 约束，并分别保留 `(session_id, plan_id, ref_type, ref_id)`、`(session_id, plan_id, contribution_id)` 与 `UNIQUE(session_id, plan_id, tool_set_snapshot_id)` 的 draft registry 约束。`ContextSelectionEntry.contribution_id` 是 request-only/overlay contribution 到 `contribution_manifest` 的唯一映射；included contribution-backed entry 必须非空，按该 ID 取得正文/detail、source revision、length/hash 和 ordinal，不能从 ContextRef.ref_id、detail_ref 或 hash 反推；canonical/tool_set entry 必须为 NULL。`ContextSelectionEntry.detail_ref` 仅可出现在 `included=true` 且 `ref_type=request_only` 的 sealed entry，且必须解析到同一 `(session_id, assembly_id)`；canonical entry 不得伪造 detail_ref。`ContextAssemblySnapshot` 必须保存 selection 的不可变副本、`ref_manifest[]`、`contribution_manifest[]` 和 `tool_set_manifest[]`，三种 projector 与 history 只能消费该副本/其 Saver 返回的等价 view，不得把 registries 重新 prepend、排序或拼接。跨 session fork 必须新建 target-local plan/assembly/ref/detail/contribution identity；source `(session_id, plan_id, assembly_id, ref_id, contribution_id)` 只保留在 fork lineage/audit，不得成为 target operational lookup。`included=false` 仅允许 optional source omission，必须有 `omission_reason`、`loss[]` 和 tagged source ref；可得的 identity metadata 可以保留，但 omitted `canonical_history` 与 `tool_set` entry 的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为 NULL；只有 omitted `request_only`/`overlay` entry 才可保留已有 `contribution_id`，且必须与同一 manifest 一致，不得新分配 `contribution_id` 或 `contribution_ordinal`，不得触发正文/detail 读取，没有既有映射则为 NULL。其它 `source_revision`、`content_length` 和 hash token 可以为 null/未分配，已知值必须与 manifest 一致。omitted canonical entry 只保留历史 identity/loss，omitted request-only/overlay entry 不解析 contribution/detail，omitted tool_set entry 不投影 tools；projector/restore/history 均跳过正文/工具定义，不从当前 source 回退或生成空值。required source 的 omission/loss 只能导致 seal/dispatch rejection，optional source 才能带显式 loss 继续。

seal 校验必须逐字段比较 selection entry 与其 registry/manifest：所有 entry 的 `ref_type`、`ref_id`、visibility、protection、availability、`base_delta_role` 和 `source_overlay_epoch` 必须一致；仅 `included=true` entry 还必须比较 `source_revision`、`content_length`、`content_hash`/`redacted_stable_digest`，普通 request-only 必须比较同 assembly 的 `detail_ref` 与 source manifest，included 且 contribution-backed 的 request-only/overlay source 才必须另以非空 `contribution_id` + `contribution_ordinal` 唯一解析到同一 plan 的 contribution manifest，并由 entry/manifest 的 `assembly_id` 建立本次 sealed scope；`included=false` entry 的可选 identity metadata（仅 omitted `request_only`/`overlay` 可包含已有且非空的 `contribution_id`）若存在也必须一致，但不得新分配 contribution identity/ordinal 或触发正文/detail 读取；omitted `canonical_history` 与 `tool_set` entry 的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为 NULL，没有既有 request-only/overlay 映射则 `contribution_id` 为 NULL。`selection_kind=canonical_history|request_only|overlay_base|overlay_delta` 只能绑定 session/ref identity 相符的 `ContextRef`；ContextRef 本体不得因该绑定补写或推断 assembly_id。`selection_kind=tool_set` 必须绑定同一 plan/assembly 的 `ToolSetRef`，且 `ToolSetRef` 的 source/hash/length/protection/availability 必须完全等于 `tool_set_manifest[]`，其 `ref_id` 必须解析为 `tool_set_snapshot_id`，不得解析到 item catalog、ContextContribution 正文或普通 ContextRef，且 `contribution_id`/`contribution_ordinal` 必须为 NULL。重复 `ref_id`、重复 contribution/tool-set binding、registry 缺项、union 类型与 selection_kind 不匹配或任一字段不一致返回 `plan-order-integrity`；canonical source/manifest 不一致返回 `source-mismatch`，request-only detail/contribution 不一致返回 `detail-unavailable`。ToolSetRef 的工具 schema/config manifest 只在 Provider tools/tool-config 投影中消费；history 只保留 selection metadata/可见摘要，不把工具定义伪造成 canonical message 或 Turn item；optional omitted entry 只保留 omission/loss metadata，projector 与 restore 不把它当作已解析 source。

所有 contribution 与 ToolSetSnapshot registry entry 先进入 `ContextAssemblyLedger`，由单一 composer 按确定顺序生成包含 ContextRef/ToolSetRef tagged union 的 plan 和 provenance graph。现有 LangChain `AgentMiddleware` 可以通过一个边界 adapter 继续参与执行，但只有该 adapter 能把 plan 编译成 `ModelRequest`；adapter 生成的 messages/system message/tools 是临时投影，不能反向成为 canonical history。

ContextRequestPlan 保留 prompt contribution 的独立 ref、顺序和来源，即使目标 Provider 最终把多个 contribution 合并为一个 system/developer message。plan 必须分别携带 `history_view_revision` 与 `source_overlay_epoch`；缓存保持型 source 必须同时保留稳定 base 和有序 delta ref。历史 view 改变时只重选 canonical history，并由 reconciliation 决定复用或重新注入 overlay；合并/应用只发生在 wire projector，不能把 delta 反向写入旧 base。wire role 不能抹掉 source identity、request-only/canonical 生命周期或 omission/loss 信息。`ToolSetSnapshot` 通常编码到 Provider 的独立 tools/tool-config 字段，也不能因为工具定义与 system prompt 同属一次请求而变成历史 message。plan sealed 后不能用当前 middleware 配置静默重写旧 contribution；只能创建新的 assembly 并记录 source mismatch。

### 5.2 Agent state 与历史可见投影的边界

`MessageService.get_agent_state_messages` 是 LangChain checkpoint 的兼容/诊断快照，不是 Web 的可见历史 projection，也不是 canonical item 的第二事实源。它在把 checkpoint 的 `AIMessage` 序列化为 agent-state JSONL 时，必须保留经过规范化的有序 content carrier，包括 final assistant 中的 reasoning/text 顺序、tool call 字段和必要的 content-part identity；不能为了满足旧的 text-only consumer 而静默删除 reasoning。需要可见文本的调用必须使用 history projection：`MessageService.list`/Web history 从 canonical item 或其受约束 projection 返回 `assistant_text`，并把 reasoning 作为独立受权限控制的 thinking projection。

因此，agent-state JSONL 与 history JSON/response parts 的内容不同并不表示 checkpoint 或 item 不一致：前者服务可执行/诊断恢复，后者服务默认展示。Provider request 仍必须在 projector 边界按目标能力过滤 reasoning，而不是通过修改 agent-state 快照或 canonical item 来实现过滤。旧测试若断言 final agent-state 只有 text，只能作为 legacy compatibility mismatch 记录，不能作为 itemized canonical 事实的实现依据。

LangChain projector 的规则是：

- canonical `runtime_notice`/`compaction_summary` 与 request-only `PromptContribution` 按 plan 顺序编译为 LangChain `SystemMessage` 或 Provider 等价的 system/developer/instructions 输入；多个 contribution 可以合并到一个 wire role，但 plan 仍保留独立 ref；user input 通过 `root_input_item_id` 识别 Turn 起点，user text/attachment 编译为 `HumanMessage` 或其 content list；
- 同一 `message_group_id` 下的 `assistant_output`/reasoning carrier 可以编译到一个 `AIMessage` 的有序 `content`，但 reasoning 是否可见由 policy/capability 决定；
- model tool call 进入 `AIMessage.tool_calls`，不依赖字符串或 content text；
- tool result 进入带正确 `tool_call_id` 的 `ToolMessage`；
- 无法在 LangChain 目标中保持的 source identity、opaque payload 和 item sequence 留在 plan/metadata，不冒充正文；
- projection 输出是本次调用的临时 `list[BaseMessage]`，不回写为 canonical history；tool definitions 通过工具配置投影，不伪装成普通 message。

每个 plan 必须生成 `ContextAssemblySnapshot`，记录实际选择的 item refs、contribution refs、tool set snapshot、compiler/provider 版本、visibility/loss 和 request hash；采用缓存保持型 overlay 时还要记录 `source_overlay_epoch`、base/delta/materialization refs、source revision 和 overlay hash。它既是实时 provenance 的稳定汇总，也是未来扩展查询“这个 item 为什么出现在请求中”的入口。

若 Provider 有原生 item 请求能力，则由 ProviderItemProjector 从同一个 plan 直接编码，跳过 LangChain projector。两条路径必须共享 item selection、identity mapping 和 loss report，不能各自重新扫描 JSONL 或自行解析 provider raw chunk。

### 5.3 Projection completion gate

本节只定义不随运行变化的验收合同；当前 checkout 的 PASS、FAIL、PARTIAL/UNVERIFIED、命令、退出码和 dirty-tree 观察统一记录在 `tasks.md` 的 **Verification ledger**，本设计不复制这些易过期证据。`project_context_plan` 的完整验收必须同时满足：

- `ContextRequestPlan.refs` 中缺失或不一致的 canonical ref，以及 `tool_set_refs` 中缺失或不一致的 ToolSetRef，以明确的 context-plan 错误终止；不得跳过该 ref、继续生成一个看似完整的 wire request，或把缺失解释成空 payload；
- `ContextRequestPlan.contributions` 中的 request-only contribution 按 plan/overlay 顺序把可用正文交给 wire projector，并保留 contribution identity、source revision、visibility 以及 omission/loss；只有 `contribution_metadata` 或 hash 摘要而没有正文/损失呈现，不满足该合同；
- LangChain、native Provider 和 Web history 的 selection/order/loss 需要共享同一个 Saver-owned plan；ToolSetRef 也必须从同一 selection 进入 Provider tools/tool-config，并在 history 中保留受策略控制的 metadata 而不伪造成 message。只证明 canonical item 能生成 `BaseMessage`，不能证明 request-only contribution、ToolSetRef、detail-unavailable 和 capability loss 已闭环；
- 完成判定必须分别闭合 RFC8785/跨语言 hash、detail binding/security、plan-to-wire、三投影 selection/order/loss、overlay reuse/materialization、恢复/迁移等门槛。单个核心 E2E、局部 smoke、静态检查或 strict validate 只能作为对应 ledger 证据，不得替代其它门槛。

### 5.4 Detail security、source integrity 与统一 selection/order gate

`ContextPlanDetailStore` 的安全合同必须在实现边界强制执行，而不能只作为调用方约定。`sensitive=true` 的 detail 不得以普通可读文件写入；写入前必须完成按 detail policy 的 redaction，或交给独立的 protected/encrypted storage boundary，并在 detail metadata 中记录 protection、redaction class、availability 和 hash。受保护正文不可被 SQLite、普通 detail projection 或 hash preimage 反向复制。path resolver 从 session catalog 解析出的真实 session node 和 thread catalog 解析出的真实 thread node 开始，必须对 thread node 到精确路径 `rollout/context-plan-details/<assembly_id>/<detail_id>` 的每一级已有路径组件执行 `lstat`/等价的 no-follow 检查，拒绝任意父级或最终组件 symlink；同时用 realpath containment 拒绝 `..`、mount/symlink 解析后的 workspace/session/thread 越界。`_root`、`write` 和 `read` 都必须执行同一检查，不能只检查最终文件。`detail_ref` 只能是 `{session_id, thread_id, assembly_id, detail_id}` 的逻辑 typed reference，由 resolver 映射到该路径，不得成为第二种物理路径命名。

`seal_context_assembly(required_detail=true)` 的 pre-dispatch gate 是硬拒绝：detail 参数缺失、detail ref 不存在、不可读、保护级别不满足、source revision 不匹配、`content_hash`/length 不匹配，或只有不可用于该请求的 redaction marker 时，assembly 不得进入 sealed/ready，Provider dispatch 必须返回 `detail-unavailable`、`source-mismatch` 或对应的 detail security error。只有 `required_detail=false` 才允许以显式 omission/loss marker seal；该 loss 必须进入 plan、assembly diagnostics 和 wire capability report，不能把缺失当作空字符串或成功。sealed assembly 绑定的 detail ref/hash 一旦改变必须创建新的 assembly。

Composer、LangChain projector 和 native Provider projector 在使用 `included=true` 的 `ContextRef` 或 `ContextContribution` 前都必须从 Saver 提供的已提交 source 解析正文并校验其 `content_hash`；canonical ref 校验父 item payload hash，request-only contribution 校验 contribution/detail body hash 及 source revision。`included=false` 的 optional entry 只保留其 tagged ref 与 omission/loss metadata，不读取 source、detail 或当前 registry，也不生成空正文。重启后发现 included ref 指向覆盖后的正文、错误 source、缺失 detail 或 hash 不匹配，必须显式返回 `source-mismatch`/`detail-unavailable`，停止该 request；不得只保留 ref/hash、继续发送错误正文，也不得以 `contribution_metadata` 伪造正文已到达 wire。

顺序由 Saver-owned plan 冻结为两个层次：ledger 在已分配的 `assembly_id` scope 内为每个 included、被选入的 contribution 分配单调且持久的 `contribution_ordinal`，`ContextRequestPlan.selection` 为每个 canonical/request-only/overlay/tool-set source ref 分配不重复的 `plan_ordinal`；`assembly_id` 是该次 plan/selection 的唯一持久范围，两类 ordinal 都必须随 snapshot/ref 保存。ToolSetRef 不取得 `contribution_ordinal`，但必须取得 `plan_ordinal` 并参与同一 selection 的稳定顺序。两类 ref 还显式记录 `selection_kind`、base/delta role、source overlay epoch、visibility/protection/availability 和 omission/loss。重启后只能按 `(assembly_id, plan_ordinal)` 与 included entry 的 `(assembly_id, contribution_ordinal)` 恢复；optional omitted entry 不分配 contribution ordinal，禁止按 `created_at`、`contribution_id`、JSONL 物理邻接或 Python dict insertion order 重新猜测。LangChain、native Provider 和 Web history 必须消费同一 selection 列表；projector 可以把若干 system/developer contribution 合并到一个 wire role，ToolSetRef 则只能投影到 Provider tools/tool-config，history 只保留受策略允许的 ToolSetRef metadata，不生成工具定义 message；二者都不得把所有 request-only ref 无条件 prepend 到 canonical messages，也不得改变 base→delta 或 canonical item 的 selection 顺序。该规则同时适用于 restore、overlay、rewind 后的新 view 和 history projection。

### 6. History 与 checkpoint 只 materialize 它们需要的投影

历史 summary 只使用 SQLite 稀疏索引和目标 item offset；显式详情才读取对应 JSONL payload。默认历史不 materialize request-only prompt；未来来源详情也只通过 item/source/assembly reference 读取，不扫描整个 middleware runtime。

Turn 历史中的可展开活动采用后端权威逻辑 Item 投影。SQLite projection 必须为每个逻辑 `reasoning|reasoning_summary|reasoning_encrypted|tool_call|tool_result|compaction_summary` 返回稳定 `item_id`、`item_sequence`、同一物理 item 内的 `part_ordinal`、`created_at` 和相对上一逻辑 Item 的 `elapsed_ms`，并在 Turn 统计中返回 `item_count`、`first_item_sequence`、`last_item_sequence` 与 Turn 总时长。一个 assistant carrier 内的多个 tool call 共享物理 item/offset，但按不同 `part_ordinal` 计为多个逻辑 Item；tool call 与 tool result 分别计数。checkpoint/provider 重影只能按持久 producer identity、tool relation 和 part ordinal 在后端解析，禁止比较文本正文去重。最终 checkpoint assistant 的 `content_part_refs.id` 若指向已提交 reasoning Item 的 `metadata.block_id`（或同一 provider reasoning item identity），它只是既有逻辑 Item 的 carrier 引用，不能按最终 assistant 的 item identity 再追加一次；正文相同但上述持久引用不同的 reasoning 仍必须分别保留。

history summary/detail 都直接消费上述后端顺序；detail 仅按已命中的 item/message offset 补齐参数或结果正文，不得重新决定 identity、数量和顺序。Web 收到历史 projection 后必须整体替换该 Turn 的旧 response parts，不得与旧 summary 拼接、按 message 坐标重排或按正文去重。live Turn 尚未提交时，Web 可以按流实体 identity 临时计算时长和逻辑 Item 数；终态 history 到达后以后端统计和顺序为准，替换 live 投影，并显式报告 live/history 计数不一致，不能静默保留前端结果。

历史分页必须为每个 detail 同时返回同 revision 的后端权威 summary，且两组记录的 Turn identity、ordinal 和顺序一一对应。summary 必须强制使用 summary projection，并移除工具参数、工具结果等详情正文；Web 只能用这份后端摘要淘汰已展开详情，不能自行重新摘要。历史详情只走 canonical Turn history API，不得再为了展开历史 Turn 查询 `message.v1` availability/snapshot 或把旧实时流与 history 拼接。实时流只服务尚未终态化的当前 Turn：前后端都必须限制终态 stream cache、乱序事件、block 正文和工具正文的驻留规模，启动恢复只保留未终态 stream；越界时明确截断或要求 snapshot 恢复，不允许无界保留、静默丢失或卡死进程。Web 的跨会话 timeline 使用有界 LRU，非活动会话和详情预算超限时恢复为同 revision 的权威 summary；重新展开时再按 canonical history offset 定点加载详情。

LangGraph checkpoint 的 `messages` channel 仍可以保存或恢复 LangChain message projection，但其来源必须记录 context view、assembly/plan identity 和 projection version，且不得成为 canonical item 的第二事实源。checkpoint 可以保存执行需要的私有 middleware state，但该 state 不等于 prompt/tool canonical history；可恢复的 provenance 以 compact assembly/item reference 为准。

compaction、rewind/replay 和 fork 通过 SQLite view/range/reference 选择 item；Turn 是 UI/history 默认分页边界，但 operation anchor 可以精确落在 item 或 content part。历史 projection 可以把一个 message group 拆成 text/thinking/tool summary，而 runtime restore 仍按目标 message grouping 生成合法 LangChain messages。

rewind、replay、compaction 和 fork 只能使用已提交 canonical item/content-part 上声明过的 durable `operation_anchor`。interrupt 首先使用当前 stream 的内存 cursor/ItemDraft；只有在需要恢复或审计时，终态化过程才把停止位置落成 item/content-part reference。一个普通 `assistant_output` item 可以只有 catalog/projection，不必成为操作 anchor；一个 tool call、tool result、可恢复 streaming part 或 compaction boundary 则可以额外建立 anchor 和恢复能力标记。request-only ref 与 `pending_next_turn` runtime notice 不属于 durable context view，不能直接作为 rewind/fork 的历史边界；Turn 入口必须解析到 `root_input_item_id` 或其合法的 before/inclusive anchor。resolver 必须区分 `inclusive`/`before`、source view/branch、anchor capability 和 stale/unreachable 状态。

### 7. 旧格式只允许一次性 legacy migration/import

下文的 `accepted`、`legacy_missing_turn_id`、`legacy_turn_group_ambiguous`、`legacy_multiple_user_messages`、`legacy_orphan`、`legacy_unsupported_role` 和 `legacy_identity_conflict` 都是 v1 migration candidate 的 `candidate_status`，不是 `CanonicalItemRecord.status`、`Turn.status` 或 `ControlOutcome`；被拒绝的 candidate 只能进入迁移报告/quarantine，不能创建可运行 Turn。

v1 candidate identity 和分组状态也在本 change 内冻结：`message_sequence` 仅作为有序 source coordinate 和 user-root window 的边界依据，不能仅凭非 user 行的物理邻接推断 Turn。每个 window 必须恰好包含一个 `role=user` 行；窗口内无非空 `turn_id` 时生成 `candidate_key=legacy-missing-turn:<legacy_message_hash>`、状态 `legacy_missing_turn_id`，窗口内恰有一个非空 `turn_id` 时生成 `candidate_key=legacy-turn:<turn_id>`、状态 `accepted`，并将缺失 ID 的可迁移成员归入该 ID。窗口内有多个不同非空 ID 时状态为 `legacy_turn_group_ambiguous` 并整体拒绝；同一 ID 跨多个 user window 时状态为 `legacy_multiple_user_messages` 并整体拒绝；首个 user 前、没有 root 的记录标记 `legacy_orphan`，重复 source coordinate、message identity 冲突或无法确定窗口边界标记 `legacy_identity_conflict` 并拒绝。

window 内的 legacy role 处理同样固定，不得按物理邻接或最后一条消息猜测：`assistant`、`tool`（以及明确声明等价的旧 `function`）是可迁移的 Turn member，缺失 `turn_id` 时继承该 window 的唯一 candidate ID；`system` 和 `developer` 是 `legacy_request_context`，保留 source coordinate、payload hash 和受保护 detail/lineage reference，但不进入 Turn root/member、不能创建 Turn，也不能被静默丢弃。`system_reminder` 只有在该行已有可信的 internal/checkpoint metadata 时才映射为 Turn 外的 `runtime_notice`（`turn_scope=pending_next_turn`，`turn_id=NULL`）；没有该 metadata 时标记 `legacy_unsupported_role` 并整体拒绝 candidate。未知 role、其它 legacy role 或带有不能解释的 role/payload 组合一律标记 `legacy_unsupported_role`、保留原始行到 report/quarantine 并整体拒绝 candidate。若 `system`/`developer`/受信任 `system_reminder` 携带非空 `turn_id`，该 ID 必须与 window 唯一 candidate ID 相同，否则标记 `legacy_turn_group_ambiguous`；这些行即使 ID 相同也不成为 Turn member。整个 rollout 没有任何 user window 时，所有 system/developer/system_reminder/其它行均标记 `legacy_orphan` 或 `legacy_unsupported_role` 并只进入 report/quarantine；不得生成 root 或 synthetic acceptance。

只有 `accepted` 或 `legacy_missing_turn_id` candidate 才能进入 migration 的 synthetic identity 补全，且后续 v2 Turn 仍使用新的 target-local identity；被拒绝 candidate 的每一条 source 行都必须在 migration report 中保留归属、拒绝原因和 source coordinate。

旧 artifact 通过 SQLite manifest/database metadata 的 `rollout_format_version` 与每个 JSONL envelope 的 `format_version` 双重识别；不能只根据 `role` 或第一行形状猜测。v1 是 `format_version=1`/`record_type=message` 的 message-line，固定包含 `message_sequence`、`message_id`、`turn_id`、`role`、`message` 和 `metadata`；v2 是 `format_version=2`/`record_type=item` 的 item-line，必填非空字段固定为 `item_sequence`、`item_id`、`semantic_kind`、`payload_kind`、`status`、`producer_ref`、`payload`、`content_hash`、`created_at` 和 `metadata`，并包含固定版本/record type；`turn_id`、`turn_scope`、`message_group_id` 和 `wire_role` 仅按语义可空/可省略。两者不允许混写；manifest 与 envelope 不一致、未知版本或结构不完整时必须进入 recovery/error，而不是静默 fallback。v1 的识别结果只能交给显式一次性 `legacy_import_v1_to_v2` migration/import operation；正常 history、provider、checkpoint、Turn runtime 和 context compiler 发现 v1 时必须返回 `v1_migration_required`，不得打开 v1 reader。

`legacy_import_v1_to_v2` 是唯一允许读取 v1 message-line 的一次性 migration/import operation。它在临时 staging artifact 中将 v1 完整 message 映射为 v2 的 `CanonicalItemRecord`/projection：v1 user message 映射为 `user_input` root candidate，v1 assistant AIMessage 映射为 `assistant_output` 及其 content parts，`assistant_text` 只生成 projection，tool call/result 保留独立 identity。migration reader 可以展示临时 legacy identity，但不得把临时 identity 当成已经接受的 v2 Turn，也不得被正常 history/provider/checkpoint API 调用。旧记录没有 middleware provenance 时必须标记为 `legacy/unknown_source`；`system`/`developer` 按前述 `legacy_request_context` 保留，内部 `system_reminder` 只有依据已有 internal/checkpoint metadata 才能映射为 Turn 外的 `runtime_notice`，否则 candidate 必须以 `legacy_unsupported_role` 拒绝，不得根据当前 middleware 配置伪造历史来源。

显式 migration 在临时 v2 artifact 中完成 JSONL、SQLite catalog/view/Turn/assembly 校验后，才原子安装新的 v2 session；v1 原 artifact 保持不变，仅供 migration reader、报告、quarantine 和回滚审计读取，不能作为正常 v1 runtime 数据源。迁移必须保留 `legacy_source_ref`、原始 message identity、Turn root confidence、tool identity、final item identity、branch/view、operation anchors 和 reasoning protection 状态；无法无损映射时保留 raw/extension reference 并标记 loss/partial。新 writer 只写 v2，不维护永久 dual writer、dual projector、双 schema 或双事实源。

v1 root candidate 的分组规则固定如下。先按 source JSONL 的 `message_sequence` 升序读取并保留每行的原始坐标，再按每个 `role=user` 行切出 user-root window，窗口延伸到下一条 user 行之前；每个窗口天然必须恰好有一个 user message。窗口内收集所有非空 `turn_id`：没有非空 ID 时生成一个 `legacy_missing_turn_id` candidate；恰有一个非空 ID 时使用该既有 ID，并只把 assistant/tool/function 等可迁移 Turn member 的缺失 ID 归入该 ID；system/developer 不进入 Turn，受信任的 system_reminder 也不进入 Turn，而是按 role 规则映射/保留；超过一个不同非空 ID 时，窗口整体拒绝并报告 `legacy_turn_group_ambiguous`。重复同一 `turn_id` 出现在一个窗口的可迁移 assistant/tool/function 行上是同一 Turn 的正常成员，不是重复 Turn。

role 归属不能从物理邻接或最后一条消息猜测：`assistant`、`tool`、明确等价的旧 `function` 才能成为 Turn member；`system`、`developer` 必须作为 `legacy_request_context` 保留 source coordinate、payload hash 和受保护 detail/lineage reference，不能创建 root/member 或被静默丢弃；有可信 internal/checkpoint metadata 的 `system_reminder` 映射为 Turn 外 `runtime_notice`（`turn_scope=pending_next_turn`、`turn_id=NULL`），缺少 metadata 时标记 `legacy_unsupported_role` 并整体拒绝 window；unknown 或其它 legacy role 同样标记 `legacy_unsupported_role`、保留原始行到 report/quarantine 并整体拒绝。system/developer/受信任 system_reminder 的非空 `turn_id` 若不等于窗口唯一 candidate ID，改报 `legacy_turn_group_ambiguous`；即使相同也不成为 Turn member。没有任何 user window 时所有记录只能进入 `legacy_orphan` 或 `legacy_unsupported_role` report/quarantine，不生成 Turn 或 synthetic acceptance。

完成窗口候选后，再按 candidate 的非空 `turn_id` 合并校验：同一非空 `turn_id` 出现在多个 user-root window，意味着一个 legacy Turn 有多个 user message，整个 ID 组拒绝迁移为 v2 Turn，不得任选第一条或拆成多个 Turn。首个 user 之前的记录没有 root，保留为 `legacy_orphan`，不得创建 Turn；其中带非空 `turn_id` 的记录也不能反向制造 root。重复 source coordinate、同一 message identity 映射到不同 turn_id，或窗口边界无法由合法 user message 确定时，相关 candidate/ID 组整体拒绝。不同窗口即使 payload 相同也不得合并。只有恰好一个 root user message 且通过上述冲突校验的 candidate 才能补齐 accepted ingress/execution identity。

v1 没有 acceptance-time ingress、execution 或可靠 final marker 时，迁移必须按每个候选 Turn 的稳定 legacy 坐标生成身份补全，而不是填随机值或猜测终态。先将 v1 root 的精确解码值组成 `{ "source_session_id": <source session>, "message_sequence": <root sequence>, "message_id": <root message id>, "role": <legacy role>, "message": <legacy message> }`，按 JCS/SHA-256 生成 `legacy_message_hash=sha256:jcs:v1:<64位小写hex>`；再对包含 source coordinate 和该 hash 的对象生成 `legacy_seed_hash=sha256:jcs:v1:<64位小写hex>`。随后固定生成 `accepted_ingress_id=legacy-ingress:<legacy_seed_hash>`、`acceptance_idempotency_key=legacy-migration:<legacy_seed_hash>`、`initial_execution_id=legacy-execution:<legacy_seed_hash>`，并将 `identity_origin=legacy_synthetic`、seed 和 source coordinate 写入 lineage metadata。源 v1 已有的 `turn_id`/message identity 只作为 `legacy_source_ref`，目标 v2 仍分配新的 target-local `turn_id`、root `item_id`、`item_sequence` 和 execution identity；seed 冲突或同 key 对应不同 payload 必须停止迁移。

迁移生成的 Turn 只有在 v1 manifest/checkpoint 或明确的 legacy final marker 能无歧义指向同一 Turn 下 `status=completed` 的 assistant output 时，才设置 `final_item_id` 并标记 `completed`；否则 `final_item_id=NULL`，Turn 标记为 `unknown`（若有明确中断/失败 marker 则使用对应状态），绝不能把最后一条 assistant message 当作 final item。没有证据的 execution outcome 记为 `unknown`，不得宣称已成功执行。上述 synthetic acceptance/execution identity 仅用于幂等迁移和审计，不表示历史中真实发生过新的 ingress 或 provider call。

新 writer 只写 v2 item 格式；正常 runtime 读取失败不得静默回退旧 writer 或 v1 reader。只有 `legacy_import_v1_to_v2` 在显式 migration/import 期间可以读取 v1，并且其 reader/report/quarantine 必须与 v2 runtime import 图隔离。回滚只允许删除或隔离尚未原子安装的 v2 staging、保留 v1 原 artifact 和 migration report 供审计；不得切换旧读取开关、重新启用 v1 history/provider/checkpoint 路径，也不得删除已安装的 v2 history。

### 8. OpenSpec 生命周期与实现顺序

这份 change 是新架构演进，不修改已完成 change 的历史 artifact。实现完成前必须验证 v2-only item storage、projection、history、branch/recovery 和独立的一次性 legacy import；必须完成旧聚合职责与临时 import shim 的删除审计，再把 delta spec 同步到主 spec。保留 v1 原 artifact 只用于 migration/rollback audit，不等于保留旧运行代码；删除旧运行路径和 shim 是 7.5 与 change 完成的必要门槛，而不是后续可选讨论。

### 9. SessionThread 是所有可执行上下文的 owner，GraphBinding 可重建但不可序列化

产品层身份固定为：

```text
workspace_id
└── session_id                         # 导航、共享资源、一个 main_thread_id
    ├── thread_id (kind=main)
    └── thread_id[] (delegated | specialist | service)
```

`SessionThread` 是 durable identity，不是 LangGraph 的 `checkpoint_ns`，也不是 Session 的显示别名。每个 Session 恰有一个不可变 `main_thread_id`；产品级“向 session 发送消息”是明确解析到 main thread 的便捷入口，内部 dispatch、history、checkpoint、rewind、compaction、ToolSet 与 subagent API 必须携带精确 `(session_id, thread_id)`。Turn、item、execution、model call、assembly、source registration、active view 和 fork anchor 都是 thread-local；其 local id 只在 owner thread 内唯一。`GlobalEntityRef` 相应升级为 `(session_id, thread_id, entity_type, local_id)`。

每个 thread 有独立的 rollout/context node，但主 thread 与其它 durable thread 使用不同分桶策略：

```text
<resolved-session-node>/
├── session.json
├── thread-catalog.json
├── threads/
│   ├── <main_thread_id>/
│   │   ├── rollout/
│   │   └── runs/
│   └── YYYY/MM/DD/<sha256(thread_id)[0:2]>/<thread_id>/
│       ├── rollout/
│       └── runs/
└── children/                           # 只承载产品级子 Session，不承载 thread
```

主 thread 的目录叶名严格等于 `main_thread_id`，直接位于 `threads/` 下；不得使用 `threads/main/` 别名。delegated、specialist、service 等其它 durable thread 根据不可变 `created_at` 的 UTC 日期分桶，并使用 `sha256(thread_id)` 前两位小写 hex 作为 shard；移动或重命名 thread 不得改变已提交 locator。session 节点的权威 thread catalog 保存 main pointer、每个 thread 的受校验相对 locator、kind、created_at、parent thread/delegation lineage、状态和 `GraphBinding`。即使主 thread 路径可预测，调用方也必须通过 catalog/resolver 定位；不得扫描目录、按日期猜测、使用显示名，或把物理树提升为第二权威。catalog 是控制 metadata，不能成为第二个 canonical writer。

附件正文不再放入 session/thread 节点。workspace 级内容寻址 store 固定为：

```text
<workspace>/.boxteam/attachments/
├── catalog.sqlite
└── YYYY/MM/DD/<digest[0:2]>/<blob-id>
```

日期是 blob 首次成功提交时的 UTC 日期，只用于物理分桶；`digest[0:2]` 是内容 SHA-256 的前两位小写 hex，`blob-id` 是不依赖原始文件名、扩展名或 MIME 的稳定内容身份。`catalog.sqlite` 是 `attachment_id/blob digest → relative blob locator`、长度/MIME/protection、session/thread/item reference、retention、tombstone 和 GC 的唯一权威；同一 digest 后续上传复用已有 blob 与首次 locator，不按新日期复制。删除 Session 或 thread 只释放其 reference，只有不存在任何 canonical/active execution/sealed assembly/operation pin 引用且满足 retention 后才能 tombstone 并回收 blob。模型、API、canonical item 和 Provider projector只能接收逻辑 `attachment_id`/variant reference及受控内容，不得接收物理路径。attachment catalog 不是 canonical message writer，thread rollout 中的 `attachment_ref` 仍是使用事实与 view membership 的权威。

迁移必须保留既有 canonical JSONL payload 的原始 bytes、item/Turn identity 和审计坐标；`thread_id == session_id` 只能作为一次性迁移输入，不能成为正常 runtime alias。旧 session-local attachment 必须先登记 workspace attachment/blob catalog 和 owner refs、验证 digest/length，再迁移或复用正文；完成前不得删除旧文件，失败不得产生悬空 attachment ref。

`GraphBinding` 至少保存 `graph_id`、`graph_revision`、`graph_schema_hash` 和 `capability_profile_hash`。持久化的是可验证的 factory selector，不是 Python `CompiledStateGraph`。重启时 runtime 以 binding 查找受注册的 factory 并校验 revision/hash；找不到或不匹配返回 `graph_binding_unavailable`，不能回退到当前最新图。进程内缓存最多复用不捕获 Session/Thread 的 graph blueprint/topology；需要 Session、thread、工具、provider 或执行信息的工具/middleware 通过每次 invocation 的 `ThreadRuntimeBinding` 取得，避免一个已编译图把其它 thread 的闭包带入请求。

LangGraph `configurable.thread_id` 必须传 product `thread_id`；`checkpoint_ns` 仍只表示该 thread 内 graph/subgraph 的 checkpoint namespace。短生命周期、无用户可见历史的 independent subagent 可以继续作为 per-invocation subgraph，不创建 SessionThread；需要多轮、可恢复、可展示的 delegated agent 才创建 child thread，并对同一 persistent child thread 串行执行。跨 session context/history/full-copy fork 仍创建 target Session 及其 main thread，不把 fork 表示为 child thread。

本决策是本 change 内所有旧 session-only 物理定位和逻辑引用的规范替代：此前出现的 session 根 `rollout/` 必须解释并迁移为 catalog 解析后的 thread node `rollout/`；此前 `{session_id, assembly_id, detail_id}` 等不含 thread 的 operational ref 必须升级为包含 `thread_id` 的 owner ref。历史任务台账中的旧路径仅描述当时已验证实现，不证明本节新增迁移已经完成。

## Risks / Trade-offs

- [LangChain grouping 丢失 item 顺序或 source identity] → canonical `item_sequence` 永远是权威；projector 记录 `message_group_id` 和 loss report，并用混合 text/reasoning/tool fixture 验证恢复。
- [每个语义 item 增加 JSONL/SQLite 索引数量] → 不落 raw chunk；使用稳定 item offset、批量 projection 和有界读取，保留现有 Turn 分页与按需详情。
- [JSONL 与 SQLite 跨文件提交仍可能留下尾部] → 继续使用 committed offset、hash 校验和启动恢复；未收敛尾部不可被任意 reader 看到。
- [旧 session 映射时存在不可逆 provider 字段] → 只在显式 `legacy_import_v1_to_v2` staging 中读取 v1；无法无损映射时保留 extension/reference 并标记 partial/loss，禁止让正常 v2 runtime 回退到旧数据或宣称自动迁移成功。
- [reasoning 或 tool payload 泄露] → item payload、history projection、Provider replay 和前端响应使用独立 visibility/protection policy；encrypted/opaque 默认只返回 marker。
- [新 item 层与现有 block stream 重复建模] → block stream 只负责实时增量和恢复 snapshot，canonical item 只负责终态事实；两者共享 normalized identity，不互相作为事实源。
- [实时 provenance 只存在内存，重启后无法展开详情] → 当前 Turn 使用内存 ledger 提供低延迟关联，并在 assembly open/stream snapshot/item finalization 时持久化 compact reference、hash 和 detail availability；无法恢复的部分明确标记 unknown，不伪造来源。
- [所有 item 都建立宽索引导致 SQLite 膨胀] → 每个 item 只要求最小 catalog，关系、projection、parts 和 operation anchors 按能力/用途建立；普通历史和操作只读取对应层。
- [细粒度 anchor 落在不可恢复的 payload 内部] → anchor 必须声明 `content_part`/fragment 的定位、hash、inclusive/before 和恢复能力；没有该能力的边界返回明确错误，不静默退回 message 末尾。
- [middleware/toolset 在 retry 或 resume 时发生变化] → `ContextAssemblySnapshot` 保存 source/version/policy/toolset hash；重建不一致时进入显式 mismatch/loss 路径，不把新配置伪装成旧请求。
- [wire role 合并后丢失动态上下文来源] → plan 保留独立的 canonical/request-only ref、贡献顺序和关系边；wire projector 只负责编码合并，历史与详情服务不从合并后的 system message 反向推断来源。
- [Turn root 被 synthetic message 抢占] → acceptance-time 在同一提交边界建立 `TurnRecord` 和 user root item；runtime notice 使用 `pending_next_turn`/ambient scope，所有查询使用 root identity 而不是 role。
- [assembly 已发出但 terminal outcome 未提交] → sealed plan 先持久化；canonical item、Turn finalization 和 assembly outcome 采用同一收敛事务，崩溃窗口统一恢复为 `unknown/execution_lost`。
- [v1/v2 混读造成错误恢复] → manifest 与每行 envelope 双重 dispatch，版本不一致直接报错；migration 使用临时 v2 artifact 和原子安装，永不覆盖 v1 原件。

## Migration Plan

1. 先冻结 `CanonicalItemRecord`、`TurnRecord`、`ExecutionRecord`、`ModelCallRecord`、`ContextRef`、`storage_commits`、`semantic_kind`、`payload_kind`、`producer_ref`、`turn_scope` 和 v1/v2 envelope dispatch/hash 合同，并声明 v2 domain/storage/runtime/projection 是唯一生产运行时事实源。
2. 在所有 v2 Session 上原子建立 thread catalog 与一个 main `SessionThread`，把原 rollout/index/checkpoint 控制状态迁入该 main thread node；保持 canonical JSONL bytes、item/Turn id、offset 和 source lineage 不变，并将旧 session-as-thread 值仅记录为 migration lineage。
3. 更新 checkpoint config、storage resolver、ContextStore/CSM/ToolSet owner、history/operation API 和 execution dispatch，使每一个操作要求 `(session_id, thread_id)`；禁止用 `checkpoint_ns` 或当前 process cache 推断 product thread。
4. 将 graph factory 改为可由 `GraphBinding` 重建且以 invocation binding 注入 session/thread 依赖；在此之前不得跨 thread 复用捕获 session 的 compiled graph。
5. 新 delegated agent 写入 parent Session 的 child thread；历史 delegated Session 保持独立且记录迁移候选，不能静默改写产品导航树。
6. 冻结 acceptance-time Turn root、ingress idempotency、global Turn/branch identity、execution/model-call/retry/resume identity、`final_item_id` 和 pending runtime notice 的 view/assembly 语义。
7. 增加新格式的 reader/index/validator 与 deterministic fixture，先验证 user root、runtime notice、assistant output/content part、tool causality、reasoning protection、provenance、两阶段 commit、metadata-only outcome、assembly snapshot 和崩溃恢复。
8. 将现有 LiteLLM normalized block/delta 接到 ItemDraft 和 canonical writer，同时保留现有实时 message stream；不要从最终 AIMessage 反向猜实时 item，也不要把 `assistant_text` 当 canonical kind。
9. 让 `RolloutCheckpointSaver` 成为已提交 context view/plan/snapshot 的唯一 owner，再实现 ContextContribution composer、HistoryProjector、LangChainProjector 和 ProviderItemProjector；分别验证 prompt/tool provenance、system wire role 合并、hash mismatch、summary/detail、LangGraph restore、tool loop、native item request 与显式 loss。
10. 接入 checkpoint、compaction、interrupt、rewind/replay、fork 的 item/content-part view/reference/anchor 语义；验证 pending notice 不创建 Turn、旧 branch 不污染新 active view、历史 Turn 不重复复制，且 Turn root 与细粒度 operation anchor 不冲突。
11. 提供唯一的 `legacy_import_v1_to_v2` 一次性 message-line migration/import 命令和报告；正常 history/provider/checkpoint/runtime 不调用 v1 reader。迁移完成后原始 v1 artifact 只作为不可变 migration/rollback audit 保存，失败不得覆盖或删除它。
12. 完成真实/确定性 mock/provider 分层验证、v2-only import/runtime 审计、旧聚合职责与临时 import shim 删除、主 spec 同步和 OpenSpec verify；删除旧运行代码、dual writer、dual projector、双 schema/双事实源是 change 完成和 7.5 的必要门槛，不得留到“再讨论”。

## Open Questions

- SQLite item projection 的名称和 Python 类型归属已经冻结：canonical item 的最小事实索引是 `item_catalog`，关系/来源是 `item_relations`，content-part locator 是 `item_parts`，按需历史/消息投影是 `item_projections`；不得复用 `messages` 作为 canonical item 表，也不得新增未在本设计中登记的并行 item 事实表。当前聚合文件若因 import 迁移暂留，只能是有删除门槛的临时 import shim；目标类型位置以 1.1 的 `app/domain/itemized/` 和 infrastructure/mapping 目录为准，shim 最迟在 7.5/change 完成前删除。
- `content_part` 的 domain 类型固定进入 `app/domain/itemized/parts.py`，其 `item_parts` locator 由 `app/services/infrastructure/rollout_context/storage/catalog.py` 维护，JSON Pointer/part resolver 属于同一 storage catalog 边界；这些物理模块位置不改变 payload 作为唯一 canonical 正文来源、`content_hash` 输入、part locator 校验、anchor identity/hash/recovery contract 或 detail-store 敏感边界。
- 首个生产切换是否按新 session 默认、配置开关或显式迁移命令分阶段启用，延期到迁移 fixture、回滚演练和真实恢复证据完成后决定；该部署顺序不改变已冻结的 `rollout_format_version`/`format_version` 字段、legacy reader、v2 writer 不双写或 commit/replay 行为。
