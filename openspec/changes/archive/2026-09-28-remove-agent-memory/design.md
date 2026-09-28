## Context

- 基线：`git rev-parse HEAD` = `70541f6b9a370e53a630b8fab311a92508c0a9f5`。
- 取证来源（只读，已独立抽查复核）：`out/tests/temp/memory_surface_inventory/artifacts/report.md`（代码侧 A/B/C 三类 + 删除依赖图 + 测试影响面 + 8 步拓扑删除顺序）、`out/tests/temp/memory_docs_consistency/artifacts/report.md`（文档侧 D/E/F 三类 + 精确改法 + 跨 change 连带影响 + 落点建议）。
- 关键既有事实：提交 `32bc6256`（2026-09-28）已物理移除 VRN 侧的 memory 死路径（`grammar.py:17` 的 scope 闭集现为 `{workspace, gateway, builtin}`、`values.py:24` 的 `_DESCRIPTOR_KINDS` 现为 `{agent-spec, skills}`）；本 change 处理**代码侧尚未下线的 memory 功能件**与**文档侧残留表述**。
- `MemoryMiddleware` 来自第三方 `deepagents.middleware.memory`（`deepagents==0.6.2`）。deepagents 的其它能力（backends、filesystem/summarization/skills middleware 等）仍在大量使用，依赖**不可缩减**。
- 全仓检索结论：`openspec/specs/` 下 32 份 main spec 对 `memory` **零命中**；`src/` 前端**零** agent memory 触点。

## Goals / Non-Goals

**Goals:**

- 物理下线全部未接入生产的 agent memory 功能件（middleware、memory state 适配、source kind、policy key、prompt tag、配置键与 schema）。
- 消除 graph binding 对「从未真实存在的 middleware」的虚假声明，并按设计规定 bump graph revision。
- 明确登记 graph revision bump 与配置 schema 收紧带来的 fail-closed 迁移义务。
- 修正既有 change 文档中与代码（`32bc6256`）不符的 memory 表述矛盾，保留有效性不受影响的否定性登记。

**Non-Goals:**

- 不顺手动 `knowledge`/`safety` 接线：`add-context-injection-lifecycle` 已明确「当前未接线的 knowledge/safety 配置也不是生产上下文来源，不得为了完整预先实现」。本次只移除 memory。
- 不修改生产代码、测试、配置、前端：本 change 只写 `openspec/**`（proposal/design/spec delta/tasks），实施由后续执行轮次完成。
- 不删除 VRN 侧「memory 不是 VRN scope」的否定性守卫（那是有效边界裁定，且 `32bc6256` 已落地）。
- 不改 `tests/unit/services/infrastructure/resource_platform/virtual_resources/test_vrn_grammar.py:38-41` 的 `memory` 反向守卫用例。
- 不改 `docs/memory/`、`AGENTS.md:55` 的 Out of memory、`ZeroMemory`、in-memory/内存 snapshot、`chat-history-memory-bounds` 产物路径等无关同名。

## Decisions

### D1. 落点：新建独立 change `remove-agent-memory`

memory 移除横切 agents / resource_platform / config / 文档四层，与任一既有 change 的主题（上下文注入生命周期、VRN 统一寻址、会话上下文 URI 迁移、多工作区后端挂载、持久化资源管理）都不同。把它塞进任一既有 change 会稀释其主题，违反 AGENTS.md「纵向切片」与「单次改动只聚焦一条清晰垂直子链路」。故按文档清点报告 §6 的建议，新建独立 change。

### D2. graph_binding slot 处置：删除 slot 并 bump `DEEP_AGENT_GRAPH_REVISION` 1 → 2

**裁定：删除 `graph_binding.py:366` 的 `"StructuredMemoryMiddleware"` slot，并将 `DEEP_AGENT_GRAPH_REVISION`（`:346`）bump 到 2。**

理由：该 slot 本身是**虚假声明**——生产链从不传 `memory`，`StructuredMemoryMiddleware` 从未真实出现在任何图里，而 slot 让 `graph_schema_hash`（`compute_graph_schema_hash`）声称图族含它。删除使声明与实现一致。bump revision 是本仓库设计对「图定义变更」规定的正确处置。

**迁移义务（H2，MUST 如实登记，不得写成「无影响」或「预期行为待裁定」）**：删除 slot 会改变 `graph_schema_hash`；`resolve_or_persist_graph_binding`（`app/agents/graph_binding.py:666-693`）在每次构建时比对持久化 selector 的 `graph_schema_hash`（`:239`），不一致即抛 `GraphBindingUnavailableError`（`graph_binding_unavailable`）**fail-closed 且不回退到最新图、不重建**。因此**历史 workspace 的 persisted binding 将一次性失效**。这是本仓库「graph 定义变更即 fail-closed」设计下的**预期行为**，但只讲到这一步是**登记不完整**。

**真实缺口（MUST 如实登记）**：当前仓库**不存在任何自动重绑入口**——

- `JsonFileGraphBindingStore.save_graph_binding`（`app/agents/graph_binding.py:494-529`，冲突抛点 `:520`）对**同一 owner 写入不同 selector** 时抛 `RuntimeError("graph-binding-store-conflict: ... 重绑必须走显式流程，不允许静默覆盖")`；类 docstring（`:469`）自述「重绑流程属于 OpenSpec 后续轮次，本类不静默覆盖」。
- `resolve_or_persist_graph_binding`（`:666`）在解析不到精确 revision/hash 时只 fail-closed，**既不回退也不重建**。

两条相加的后果：删 slot + bump revision 后，历史 owner（**含 live 用户会话**）在无显式迁移时**永久无法继续**，`save` 也不会自动修复。这是功能性阻断，不是可含糊为「是否需工具待裁定」的预期行为。

**裁定（D-3，owner 已定）**：该一次性重绑/清理能力**必须在本 change 内登记为可实施任务**（tasks 7.3.1–7.3.3），不得留作待裁定。迁移 MUST 为一次性、幂等、显式、可审计，MUST NOT 静默覆盖（AGENTS.md「永不静默失败」、「快速失败而非优雅降级」）。存量数据范围为 live `1` 处 + `out/` 下 `19` 处（合计 `20` 处 `graph-bindings.json`），逐处处置见 tasks 7.3.2/7.3.3。

> 备注：stable prefix / 解析本身不受 slot 名影响，只受 hash 影响；slot 只是声明层，不与 invocation 实际出现的 middleware 做集合比对（`graph_binding.py:350-352` 的既有 TODO 已声明此限制）。

### D3. config 键删除与 fail-closed 后果

**裁定：删除 `agent.memory` 全套键与其 schema 定义。**

- `configs/workspace_inline.jsonc:427-433`：删除 `memory` 块（6 键）。
- `configs/workspace_schema.jsonc:1052-1054`：删除 `agentConfig.properties.memory`；`:1430-1495`：删除 `$defs.agentMemory`。
- `configs/tests/workspace/default.jsonc`：删除四处 memory 测试块（`:317-325`、`:397-399`、`:482-484`、`:572-574`）。

**fail-closed 后果（如实登记）**：schema 逐层 `additionalProperties:false`，`configs/runtime.py:40 validate_config` 直接 `jsonschema.validate`，校验失败被包装为 `ValueError("配置验证失败: ... location=...")`（`:53`）**显式报错**，不会静默忽略。且 `configs/layout_migrations.py`/`configs/legacy_migrations.py` 均不触碰 `agent.memory`/`agent.knowledge.retrieval`，无自动剔除逻辑。

**旧用户残留键处置裁定**：接受 fail-closed 报错，**不在迁移脚本中主动剔除** memory 键。理由：本地代理「快速失败、永不静默」原则；且自动剔除属于对用户配置的隐式写入，与本仓库「不悄悄改用户数据」取向冲突。该行为等价于一次 BREAKING 配置变更，MUST 在 tasks 与变更说明中显式登记，让用户在启动报错时明确看到字段位置。（若 owner 后续判定需要平滑迁移，可作为独立议题另议，本 change 不预先实现。）

**`agent.knowledge.retrieval` 边界**：该 4 键与 `agent.memory` 同为「零读取的未接入占位」，但按 D6（非目标）本次**只移除 memory**；`agent.knowledge` 的 `enabled`/`sources`/`retrieval` 是否一并清理**留待 owner 另行裁定**，本 change 记为显式待裁定项。

**`enable_workspace_memory`（`default.jsonc:333`）**：`feature_flags` 是开放布尔 map 且 `app/` 零读取；它是 memory 命名的死 flag，与已删除的 `agent.memory` 属同一类死配置，不属于 B/E 类无关同名（非 `ZeroMemory`/in-memory/`docs/memory/` 等）。**裁定：owner 已裁定删除并落地（提交 1488b8f7 删除 `configs/tests/workspace/default.jsonc:333` 该行；同段 `enable_kb`/`enable_code_tools` 与其它 agent 的 `feature_flags` 段不动，`app/`/`src/` 对该键本为零读取，删除不改变运行时行为）。**

### D4. 灰色的 `agent_memory` policy key / tag（`untrusted_reference` 死值已按裁定删除）

**裁定：一并删除 `agent_memory` policy key 与 prompt tag；同时删除因此成为死值的 `PromptTrustLevel.untrusted_reference`。（已落地：见提交 e0c45e4f 删除 `app/prompting/registry.py` 的 `PromptTrustLevel.untrusted_reference`，全仓 `rg 'untrusted_reference' app configs tests` 零命中；本项已不再是待裁定项。）**

- `app/agents/instruction_producers.py:43`（`ConditionalPolicyKey` 成员）、`:319`（`_POLICY_KEYS` 成员）、注释 `:3`/`:140` 的「R01–R07/R09」「R09 memory」措辞 → 删除。该 key 从不被 `build_toolset_policy_keys`（`:178` 定义，体在 `:187-197`；原登记 `:186-206` 区间不准，已修正）产出，删除不改变运行时行为。
- **代码侧 R09 残留（M2，登记为待删除项，本 change MUST NOT 改代码）**：`app/agents/instruction_producers.py:35` 的 `InstructionProducerId = Literal[... "R07", "R09"]` 与 `:311` 的 `_KNOWN_PRODUCER_IDS = frozenset({..., "R09"})` 仍含 `"R09"`。R09 即被移除的 memory 能力在 producer 身份层的编号，与 policy key `agent_memory`（`:43`/`:319`）是同一能力的两处登记；二者须随 5.1 一并物理下线。本 change 只登记精确路径行号与符号名，实施轮次再删。
- `app/prompting/registry.py:227-233` 的 `PromptTagSpec("agent_memory", ...)` → 删除。其唯一消费者是被删的 `StructuredMemoryMiddleware`。
- **`PromptTrustLevel.untrusted_reference`（`registry.py:17`）**：全仓仅 `registry.py:17`（定义）与 `:229`（`agent_memory` tag 使用）两处引用（已 `rg` 确证）。删除 `agent_memory` tag 后该枚举值**再无引用**，成为死值。

  **我倾向的处置：一并删除 `untrusted_reference`。** 理由：AGENTS.md「坚定做代码减法」与「彻底根除双轨」；保留无引用的枚举值是技术债。**但我把这一点列为需要 owner 确认的裁定项**：`PromptTrustLevel` 是通用 trust level 词汇，未来若有新的「不可信参考数据」类 source 需要该级别，删除后需重新新增；且 tag/enum 顺序可能被结构化提示契约测试或 stable prefix 依赖。**落地时 MUST 先运行结构化提示相关测试确认删除不导致 red**，再决定是否删除枚举值。

  **若 owner 选择保留**：保留枚举值但在本 change 的 spec delta/tasks 中显式登记「`untrusted_reference` 为无引用保留值，保留理由是 xxx」，不得含糊过去。

### D5. `memory_state` source kind 移除

**裁定：从 `app/services/infrastructure/resource_platform/sources/observed_source.py:28` 的 `_SOURCE_KINDS` 删除 `"memory_state"`，并同步修正 `:87` docstring 与 `:122` 错误文案为只提 gateway。**

生产只构造 `"file"`（`workspace_file_resources.py:107-109`）；`"gateway_snapshot"`/`"memory_state"` 仅测试构造。删除后 `_SOURCE_KINDS` 允许集合更严（fail-closed 更紧），符合「快速失败」。

**相邻技术债**：`"gateway_snapshot"` 同属「未接线的 token 来源」实现。**裁定：本 change 保留 `gateway_snapshot`**（任务只要求清 memory）。是否连带回收 `gateway_snapshot` 登记为待裁定项（见 D7）。

**测试覆盖保护**：`tests/unit/services/infrastructure/test_source_reconciler.py:165-187` 的 `test_token_source_error_retains_previous_revision` 用 `memory_state` 验「token 来源失败保留上一 revision」。裁定：**改为 `gateway_snapshot`**（该文件 `:142` 已有同类构造），保留该 token 语义分支覆盖，不得直接删用例造成覆盖退化。

### D6. 不实现 `knowledge`/`safety` 的边界

`add-context-injection-lifecycle` 已明确「当前未接线的 knowledge/safety 配置也不是生产上下文来源，不得为了完整预先实现」。本 change 严格只移除 memory，不触碰 `configs/workspace_inline.jsonc` 的 `agent.knowledge.*` 与 `agent.safety.*`，也不触碰 `app/prompting/registry.py` 中与 memory 无关的 tag。

### D7. 显式待裁定项（MUST NOT 在本 change 内擅自替 owner 决定）

1. ~~`PromptTrustLevel.untrusted_reference` 是否回收~~（D4）：**已裁定删除并落地**（提交 e0c45e4f；全仓零引用），**不再是待裁定项**。
2. **`agent.knowledge.retrieval` 等 4 键是否一并清理**（D3）：**保留待 owner 裁定**。它非 memory 能力（`add-context-injection-lifecycle` 的 D6 明确「不得为完整预先实现 knowledge」），本次留作未变。
3. **`gateway_snapshot` source kind 是否一并回收**（D5）：本次保留。
4. ~~`configs/tests/workspace/default.jsonc` 的 `enable_workspace_memory`~~（D3）：**已裁定删除并落地**（提交 1488b8f7；`app/`/`src/` 对该键零读取），**不再是待裁定项**。
5. ~~`docs/middleware-prompt-vscode-comparison.html` 的 `MemoryMiddleware` 整卡~~（D7 第 5 项）：**已裁定整卡删除并落地**（提交 1488b8f7；该卡描述的 `StructuredMemoryMiddleware`/`MEMORY_SYSTEM_PROMPT`/`untrusted_reference`/graph slot 均已物理删除，且徽章「仅配置启用时」暗示其仍可启用属失实；删除后 `grep -n -i 'memory' docs/middleware-prompt-vscode-comparison.html` 零命中，周围卡片与 HTML 结构完好），**不再是待裁定项**。

> **原第 6 项「历史 workspace graph binding 的重绑流程」已按 owner 裁定（D-3）移出待裁定列表**：重绑能力**必须在本 change 内登记为可实施任务**（tasks 7.3.1–7.3.3），见 D2 的真实缺口登记。

### D7b. 跨 change 同步项的实际执行（M1 例外）

本 change 的既定范围是只写 `openspec/changes/remove-agent-memory/**`。经 owner 裁定，R09 集合收窄属**跨 change 同步项**，允许在本 change 内对 `add-context-injection-lifecycle` 做**最小改动**（只加约束与具名引用、不重写整体设计）：

- `add-context-injection-lifecycle/tasks.md:96`：`R01–R09` → `R01–R07`（加注 R09 已随本 change 移除）。
- `add-context-injection-lifecycle/design.md:514`：`R01–R07 以及显式启用的 R09` → `R01–R07`（加注 R09 已移除）。
- `add-context-injection-lifecycle/design.md:668`：`按R01–R09、E01–E07` → `按R01–R07、E01–E07`（加注 R09 已移除）。

改动后的精确路径与行号登记在 tasks 10.4.2；`add-context-injection-lifecycle` 的其余 R09 引用（`design.md:469/:484`、`spec.md:594`、`tasks.md:59`）仍按 10.4 的跨 change 同步项登记，交由该 change 后续处理。

### D8. 跨 change 文档同步项的范围判定

`openspec` 惯例下，各 change 的 `proposal.md`/`design.md`/`tasks.md` 与 delta spec 由**该 change 自己**持有并修订；一个 change 不应改写另一个在途 change 的文档正文（多个 agent 共用工作树时尤其危险）。故：

- `add-unified-virtual-resource-addressing` 的 `spec.md:211`/`proposal.md:19`/`tasks.md:7` 称描述符闭集「含 memory」与代码 `32bc6256` 不符——**不在本 change 内直接改这些文件**，而是在本 change 的 `tasks.md` 中登记为**跨 change 同步项**（含精确路径与行号），交由该 change 或后续同步轮次处理。
- **保留性约束**：`add-unified-virtual-resource-addressing` 中「memory 不是 VRN scope」类否定性登记 MUST **保留**，仅把「同期由独立代码切片落地」改为已落地口径（「已由 32bc6256 落地」）；其中「domain owner 与状态本体从未接入，故无 VRN 替代 owner 的需求」这一**结论句 MUST 原样保住**——`add-multi-workspace-backend-mounting/design.md:139` 逐字引用了它，改写会使其失准，只改其后的落地时态。
- 同类待同步项（均由文档清点报告 §2 给出精确改法）：`migrate-session-context-uri-to-vrn` 的 `design.md:15/:29/:108`（scope 闭集仍写含 memory）、`add-context-injection-lifecycle` 的 R09 整行与 `design.md:5/:178/:248/:328/:355/:469/:484`、`proposal.md:37/:58`、`spec.md:594`、`tasks.md:32/:43/:59`、`add-itemized-rollout-context` 的 `design.md:195/:475`、`spec.md:811/:821/:1118/:1132/:1246`、`checkpoint-history-loading/spec.md:203`（原登记 `:260` 已因提交 `b65f15b1` 移位失效，M3 已修正为精确行号）、`tasks.md:37/:167`。

  **R09 集合收窄三处已按 owner 裁定在本 change 内直接执行**（原登记遗漏，M1）：`add-context-injection-lifecycle` 的 `tasks.md:96`、`design.md:514`、`design.md:668`，见 D7b 与 tasks 10.4.1/10.4.2。

### D9. spec delta 落点与编码

经检索，`openspec/specs/` 32 份 main spec **零 memory requirement**；被移除的 memory 能力从未进入任何 main spec，只存在于未接线代码与未归档 change 的 delta 描述中。经隔离复制实测（`/tmp` workbench）：对**尚不存在于 main specs 的 capability** 使用 `## REMOVED Requirements` 是**warned no-op**（`"N REMOVED requirement(s) ignored for new spec (nothing to remove)"`），且 REMOVED-only 会让重建后的 spec 零 requirement 而在 archive 时 **`archive_spec_validation_failed`**。

**裁定（D-1，owner 已定）：改用 ADDED-only 形式。** 原混合形式（新 capability + `## REMOVED Requirements` 具名登记 + ADDED 负向边界）理由成立但属**取巧**：`## REMOVED Requirements` 在 openspec 中本义是「从**已存在**的 spec 删除 requirement」，而本 change 把它当作「被移除能力的具名登记」，归档时 REMOVED 段被全部丢弃（warned no-op），与语义不符，也留不下记录。

因此本 change 的 delta **只保留 `## ADDED Requirements`**：在 `specs/agent-memory/spec.md` 里以负向边界 requirement（含可验证 scenario）承载「agent memory 必须不存在」。归档后 main spec 会**持久保留**这条有效边界（ADDED 会写入主 spec），而 REMOVED 会在归档时丢弃。

**「移除了哪 7 条能力」的完整记录留在 `proposal.md` 与 `design.md`**（设计文档本就是记录决策的地方），不在 delta 中重复：`proposal.md` 的 What Changes/Impact 逐条列出被移除的 memory 链路与配置；本 design 的 D2–D6 记录每一条移除的理由与真实符号对应。

> 实测：新形式通过 `openspec validate remove-agent-memory --strict` 与 `--strict --all`（0 failed），并已实测可归档（见本 change 交付报告 `out/tests/temp/memory_change_fixer/artifacts/report.md` 的归档验证输出）。

## Risks / Trade-offs

- **R1 编译期悬空引用**：删 `structured_memory_middleware.py`/`MEMORY_SYSTEM_PROMPT`/`memory_state.py` 而漏改 import 会 `ImportError`。缓解：按依赖图逐条核对 `deep_agent_stack.py:31,41`、`middleware_prompts.py:87`、`bootstrap.py:31-33`、`adapters/__init__.py`。
- **R2 调用期参数不匹配**：删 `build_deep_agent_middleware`/`create_my_deep_agent` 的 `memory` 形参而漏改 `agent_factory.py:813` 会 `TypeError: unexpected keyword argument 'memory'`。缓解：先删调用点再删定义。
- **R3 配置 fail-closed**：删 schema 键后旧用户残留 memory 键会启动报错。属预期行为，但等价 BREAKING；已登记为迁移义务。
- **R4 持久化契约**：graph revision bump 使历史 persisted binding fail-closed。属预期行为，已登记为迁移义务；代价是历史 workspace 需重绑。
- **R5 测试覆盖退化**：`test_source_reconciler.py:165-187` 若直接删而非改用 `gateway_snapshot`，会丢失 token 来源保留分支覆盖。缓解：改造而非删除。
- **R6 文档/在途 change 不一致**：`docs/middleware-prompt-vscode-comparison.html` 与多个在途 change 仍描述 memory。缓解：登记跨 change 同步项；`docs/middleware-prompt-vscode-comparison.html` 的 `MemoryMiddleware` 整卡已按 owner 裁定删除并落地（提交 1488b8f7）。
- **R7 stable prefix**：`agent_memory` 作为 `PromptPlacement.system_prompt` root tag，其渲染顺序若进 stable prefix，删除可能影响前缀。缓解：落地时显式运行结构化提示契约测试验证。
