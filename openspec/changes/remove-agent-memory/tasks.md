## 1. 测试引用层收敛（最外层，先让引用收敛）

> 目的：先删/改测试引用，使 `rg` 残留引用即等于待删生产符号清单（AGENTS.md「废除旧接口前先全仓检索调用方与测试用例」）。每步后跑该层 focused 测试。

- [x] 1.1 `tests/unit/agents/test_middleware_prompts.py`：删除模块级 memory 相关 import（`MemoryMiddleware`、`MEMORY_SYSTEM_PROMPT`、`StructuredMemoryMiddleware`，约 `:8/:27/:36`）、`_build_middleware` 的 `memory` 形参（`:69/:87`）；`test_middleware_uses_project_prompts_without_upstream_demo_agents`（`:98`）删去 `memory=["/memory.md"]` 实参与 `agent_memory` 断言（`:121-122`），保留其余断言；整条删除纯 memory 用例 `test_memory_content_uses_registered_system_prompt_section`（`:134-144`）；`test_project_middleware_prompt_budget_stays_small`（`:179`）删去 `memory=["/memory.md"]`（`:180`），保留预算断言。验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/agents/test_middleware_prompts.py -q`。
- [x] 1.2 `tests/unit/agents/policy/test_tool_policy.py`：删除调用 `build_deep_agent_middleware` 处的 `memory=None,`（`:187`）。验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/agents/policy/test_tool_policy.py -q`。
- [x] 1.3 `tests/unit/services/infrastructure/resource_platform/test_bootstrap.py`：删除 `_FakeMemoryReader`（`:40-48`）、memory 相关 import（`:17-19`）、`test_bootstrap_assembles_platform_with_fixed_adapters` 中的 `memory_state_reader=`/`memory_state_keys=`（`:59-60`）与 `platform.memory_states` 断言（`:64/:70`）；整条删除 `test_memory_state_adapter_rejects_unregistered_key`（`:180-188`）。验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/services/infrastructure/resource_platform/test_bootstrap.py -q`。
- [x] 1.4 `tests/unit/services/infrastructure/test_source_reconciler.py`：`test_token_source_error_retains_previous_revision`（`:165-187`）把 `source_kind="memory_state"` 与 `display_uri="boxteam://memory/team"` **改为 `gateway_snapshot` 同类构造**（参照同文件 `:142`），保留「token 来源失败保留上一 revision」分支覆盖，MUST NOT 直接删用例。验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/services/infrastructure/test_source_reconciler.py -q`。
- [x] 1.5 `tests/unit/agents/test_instruction_producers.py`：`test_unwired_producer_cannot_pose_as_enabled` 附近以 `source_kind="agent_memory"`、`policy_key="agent_memory"`（`:88-100`）举例；随 D4 裁定删除 `agent_memory` policy key 后，改用其它未接线 producer 举例，保留「未接线 producer 不得伪装启用」断言。验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/agents/test_instruction_producers.py -q`。

## 2. 删除 memory middleware 链（agents 层）

- [x] 2.1 删除整文件 `app/agents/structured_memory_middleware.py`（`StructuredMemoryMiddleware`）。
- [x] 2.2 `app/agents/middleware_prompts.py`：删除 `MEMORY_SYSTEM_PROMPT`（`:66-68`）及其 `__all__` 导出项（`:87`）。
- [x] 2.3 `app/agents/deep_agent_stack.py`：删除 memory 相关 import（`:31` 的 `MEMORY_SYSTEM_PROMPT`、`:41` 的 `StructuredMemoryMiddleware`）、形参 `memory: list[str] | None`（`:130`）与 `if memory:` 分支（`:202-208`）。
- [x] 2.4 `app/agents/agent_factory.py`：删除 `create_my_deep_agent` 的 `memory` 形参（`:419`）与唯一透传 `memory=memory,`（`:813`）。
- [x] 2.5 复核生产链无残留 memory 实参：`create_runtime_deep_agent_for_session`（`agent_factory.py:951-1001`）与 `app/runtime/agent_runtime.py:120` 确认不传 `memory`。验证：`rg -n "memory" app/agents app/runtime` 应零 A 类命中（仅允许无关同名）。
- [x] 2.6 验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/agents -q`；静态分析：`uv run python -m compileall -q app/agents app/runtime`（仓库 `pyproject.toml` 未配置 ruff/lint 工具，故不做 ruff check；`uv run ruff check ...` 会因未安装 ruff 直接失败，不得写入验证命令）。

## 3. 删除 memory state 适配链（resource_platform 层）

- [x] 3.1 删除整文件 `app/services/infrastructure/resource_platform/adapters/memory_state.py`（`AuthoritativeMemorySnapshot`/`MemoryStateReader`/`MemoryStateAdapter`）。
- [x] 3.2 `app/services/infrastructure/resource_platform/adapters/__init__.py`：删除三个符号的 re-export 与 `__all__` 项（`:17-20/:25/:33-34`）。
- [x] 3.3 `app/services/infrastructure/resource_platform/bootstrap.py`：删除 import（`:31-33`）、`ResourcePlatform.memory_states` 字段（`:62`）、`memory_state_reader`/`memory_state_keys` 形参（`:104-105`）、`memory_states = MemoryStateAdapter(...)` 装配（`:152-156`）与结果字段 `memory_states=memory_states`（`:166`）。
- [x] 3.4 复核 `app/container.py:476` 无需改动（本就没传 `memory_state_reader`）。验证：`rg -n "memory_state|memory_states|MemoryState" app` 零命中。
- [x] 3.5 验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/services/infrastructure/resource_platform -q`。

## 4. 删除 memory_state source kind

- [x] 4.1 `app/services/infrastructure/resource_platform/sources/observed_source.py`：从 `_SOURCE_KINDS`（`:28`）删除 `"memory_state"`；同步修正 `:87` docstring 与 `:122` 错误文案，改为只提 `gateway`（`gateway_snapshot` 分支 MUST 保留）。
- [x] 4.2 验证：`rg -n "memory_state" app` 零命中；`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/services/infrastructure/test_source_reconciler.py tests/unit/services/infrastructure/resource_platform -q`。

## 5. 删除 instruction producer 的 agent_memory 类型占位

- [x] 5.1 `app/agents/instruction_producers.py`：删除 `ConditionalPolicyKey` 的 `"agent_memory"` 成员（`:43`）与 `_POLICY_KEYS` 的 `"agent_memory"`（`:319`）；同步清理 `:4`/`:140` 的「显式 memory」「R09 memory」措辞。
- [x] 5.2 验证：`rg -n "agent_memory" app/agents` 零命中；`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/agents/test_instruction_producers.py -q`。
- [x] 5.3 **代码侧 R09 残留已在实施轮随 5.1 一并物理下线**：`app/agents/instruction_producers.py` 的 `InstructionProducerId`（原 `:35`）与 `_KNOWN_PRODUCER_IDS`（原 `:311`）中的 `"R09"`，以及模块 docstring（原 `:3` 的「R01–R07/R09」）与 `assert_producer_registrable` docstring（原 `:140` 的「R09 memory」）措辞均已删除。R09 是被移除 `agent_memory` 能力在 producer 身份层的编号，与 policy key `agent_memory`（原 `:43`/`:319`）同属同一能力的两处登记，二者现均已下线。验证：`rg -n "R09" app` 零命中。跨 change 文档同步项见 10.4/10.4.1/10.4.2。

## 6. 删除 prompt tag 并处置 untrusted_reference 死值

- [x] 6.1 `app/prompting/registry.py`：删除 `PromptTagSpec("agent_memory", ...)`（`:227-233`）。
- [x] 6.2 依 D4 裁定处置 `PromptTrustLevel.untrusted_reference`（`:17`）：按倾向一并删除；若 owner 选择保留，则在本 change 文档显式登记保留理由。**删除前 MUST 先跑结构化提示契约测试确认无 red**。
- [x] 6.3 验证：`rg -n "agent_memory|untrusted_reference" app tests` 符合裁定结果；`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/agents -q`；显式确认 stable prefix / 结构化提示测试不因 tag 顺序变化而 red。

## 7. 删除 graph slot 并 bump graph revision

- [x] 7.1 `app/agents/graph_binding.py`：从 `DEEP_AGENT_MIDDLEWARE_STACK`（`:366`）删除 `"StructuredMemoryMiddleware"` slot。
- [x] 7.2 `app/agents/graph_binding.py`：把 `DEEP_AGENT_GRAPH_REVISION`（`:346`）由 `1` bump 到 `2`，并同步更新 `:349-352` 及 `DEEP_AGENT_TOOL_FACE`/`DEEP_AGENT_CAPABILITY_PROFILE` 注释中的「revision 1」表述（如存在）。
- [x] 7.3 **登记迁移义务（H2，MUST NOT 写成「无影响」或「预期行为待裁定」）**：删除 slot 改变 `graph_schema_hash`（`:346 DEEP_AGENT_GRAPH_REVISION` 同步由 1 bump 到 2），`resolve_or_persist_graph_binding`（`app/agents/graph_binding.py:666-693`）比对持久化 selector 的 `graph_schema_hash`（`:239`）不一致即抛 `GraphBindingUnavailableError`（`graph_binding_unavailable`）**fail-closed 且不回退到最新图**。已如实登记，无「无影响」表述。
- [x] 7.3.1 **如实登记真实缺口（当前不存在任何自动重绑入口）**：`JsonFileGraphBindingStore.save_graph_binding`（`app/agents/graph_binding.py:494-529`，冲突抛点 `:520`）对同一 owner 写入**不同** selector 时抛 `RuntimeError("graph-binding-store-conflict: ... 重绑必须走显式流程，不允许静默覆盖")`，类 docstring（`:469`）自述「重绑流程属于 OpenSpec 后续轮次，本类不静默覆盖」；而 `resolve_or_persist_graph_binding`（`:666`）在解析不到精确 revision/hash 时只 fail-closed，**既不回退也不重建**。即：**当前仓库不存在任何自动重绑/清理入口**。后果：删 slot + bump revision 后，历史 owner（含 live 用户会话）在无显式迁移时**永久无法继续**。这是本 change 必须承担的迁移义务，不是可留待裁定的预期行为。缺口如实登记，并在 7.3.2 落地显式入口。
- [x] 7.3.2 **一次性迁移/重绑能力（本 change 内登记为可实施任务，不得留作待裁定）**：提供一次幂等的显式重绑或清理步骤，使既有 persisted binding 能重新建立到 bump 后的 selector；步骤 MUST 为显式、可审计，MUST NOT 静默覆盖（AGENTS.md「永不静默失败」、「快速失败而非优雅降级」）。至少覆盖：(a) 冲突路径 `save_graph_binding` 的 `graph-binding-store-conflict` 与「无重绑入口」的处置方式（新增显式重绑入口，或明确登记其缺失及后果）；(b) 存量数据处置说明——live 路径 `/home/hyf/.boxteams/boxteam_workspace/.boxteam/graph-bindings/graph-bindings.json`（1 个 owner，`graph_revision:1`、`graph_schema_hash:sha256:78309db966399…`），以及 `out/` 下 **20 处**含 `graph-bindings.json` 的 test/e2e/development-runtime 工作区（`out/development-runtime/boxteam-home/boxteam_workspace/` 与 19 处 `out/tests/{e2e,integration,temp}/**/workspace/`）。**已落地**：`JsonFileGraphBindingStore.rebind_graph_binding`（explicit，要求调用方声明 expected，磁盘不一致即 `graph-binding-rebind-stale-expected` 拒绝）+ `persisted_owners` 盘点 + 运维入口 `scripts/rebind_graph_binding.py`（沿用 `scripts/migrate_*.py` 惯例，幂等）；正常 resolve 路径不调用它，fail-closed 语义未削弱。存量处置：live 用该脚本重绑；`out/` 下 20 处测试产物由下一次测试跑重建，不手改。
- [x] 7.3.3 存量路径枚举（实测基线，实施前先复核）：live `1` 处 + `out/tests/e2e/**`（4 处）、`out/tests/integration/**`（12 处，含 `test_gateway_workspace_routing/secondary-workspace`）、`out/tests/temp/**`（3 处）、`out/development-runtime/**`（1 处），合计 `20` 处 `graph-bindings.json`（`find out -name graph-bindings.json` 过滤出真实 store 文件；注意排除 `out/tests/unit/agents/test_graph_binding/runtime/**/tmp/graph-bindings.json` 这类单测临时夹具）。实测复核：e2e 4 + integration 12 + temp 3 + development-runtime 1 = **20**，与规划一致；单测夹具目录另计。
- [x] 7.4 验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/agents/test_graph_binding.py -q`；（用例内部引用同一常量故自洽，重点确认无 history 夹具假设 revision=1）。实测 60 passed（含新增 5 个重绑用例）；`rg` 确认无任何测试断言 revision 数值。

## 8. 删除配置键与 schema 定义（fail-closed 收紧）

- [x] 8.1 `configs/workspace_inline.jsonc`：删除 `agent.memory` 块（`:427-433`，6 键）。
- [x] 8.2 `configs/workspace_schema.jsonc`：删除 `agentConfig.properties.memory`（`:1052-1054`）与 `$defs.agentMemory`（`:1430-1495`）。
- [x] 8.3 `configs/tests/workspace/default.jsonc`：删除四处 memory 测试块（`:317-325`、`:397-399`、`:482-484`、`:572-574`）。**`:342` 的 `enable_workspace_memory` 按 D3/D7 不动**。
- [x] 8.4 确认 schema 逐层 `additionalProperties:false` 下旧用户残留 `agent.memory.*` 键会在 `configs/runtime.py:53` 显式报「配置验证失败」；登记该 fail-closed 收紧为 BREAKING 迁移义务。
- [x] 8.5 验证：`timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest tests/unit/configs -q`；`uv run python -m configs.boxteam` 相关校验路径确认 schema 自洽。

## 9. 精确到用例名的测试删改收口

- [ ] 9.1 复核 `tests/unit/agents/test_middleware_prompts.py` 已无 memory import/用例（第 1.1 步）。
- [x] 9.2 复核 `tests/unit/services/infrastructure/resource_platform/test_bootstrap.py` 已无 `_FakeMemoryReader`/`memory_states`/`test_memory_state_adapter_rejects_unregistered_key`（第 1.3 步）。
- [ ] 9.3 `tests/unit/services/infrastructure/resource_platform/virtual_resources/test_vrn_grammar.py:38-41` 的 `test_memory_scope_is_rejected_fail_closed` **MUST 保留**（memory 非 VRN scope 的反向守卫），MUST NOT 误删。
- [ ] 9.4 按 AGENTS.md 纪律，跑测试一律带进程外保护：`timeout <秒> bash -c 'ulimit -d 4194304; exec "$@"' bash <命令>`，或 `bun run test:matrix -- --suite=<id>`。**Python 测试一律走 `uv run pytest`（unit-python suite 为 `uv run pytest tests/unit`），MUST NOT 出现对 `.py` 的 `bun test`（`bun test` 对 `.py` 不匹配任何测试文件）；`bun test` 只用于 TS/TSX（如 unit-web suite 的 `bun test src/clients/web/src`）。**
- [ ] 9.5 全量回归：`bun run test:matrix`（或等价受保护全量命令）全绿。

## 10. 跨 change 文档同步项（MUST NOT 在本 change 内直接改写别个 change）

> 范围判定见 design.md D8：各 change 自持其文档，本 change 只登记精确路径行号，交由对应 change 或后续同步轮次处理。

- [x] 10.1 登记 `add-unified-virtual-resource-addressing` 的三处「描述符闭集含 memory」矛盾：`specs/virtual-resource-addressing/spec.md:211`、`proposal.md:19`、`tasks.md:7`（该三处称 `_DESCRIPTOR_KINDS` 含 memory，而代码 `32bc6256` 已删除）。建议改法：改为「两个闭集当前均为 `agent-spec`/`skills`；描述符闭集的 memory 已物理移除，语法侧以 `unknown_scope` 拒绝」；该 requirement 名「kind 闭集定稿且描述符闭集独立不可混用」被 `migrate-session-context-uri-to-vrn` 逐字引用，**保名改体**。
- [x] 10.2 登记该 change 的落地时态改写（保留否定性登记，仅改时态）：`spec.md:46`、`proposal.md:16`、`design.md:29/:54/:172` 的「同期由独立代码切片落地」→「已由提交 32bc6256 落地」。**MUST 保住**「domain owner 与状态本体从未接入，故无 VRN 替代 owner 的需求」结论句（`add-multi-workspace-backend-mounting/design.md:139` 逐字引用）。
- [x] 10.3 登记 `migrate-session-context-uri-to-vrn` 的 `design.md:15/:108`（scope 闭集仍写含 memory）与 `design.md:29`（「走的是独立特例分支」）的同步修正；`tasks.md:6（1.2-B）` 建议标记 `[x]`。
- [x] 10.4 登记 `add-context-injection-lifecycle` 的 R09 整行删除与 R 集合收窄（`design.md:469`、`design.md:484`、`spec.md:594`、`tasks.md:59`），以及 `design.md:5/:178/:248/:328/:355`、`proposal.md:37/:58`、`tasks.md:32/:43` 的 memory 措辞清理；`:248` 的否定性登记保留并改写括注。
- [x] 10.4.1 **补齐 R09 集合收窄三处（M1）**：`add-context-injection-lifecycle` 的 `tasks.md:96`（`R01–R09`）、`design.md:514`（`R01–R07 以及显式启用的 R09`）、`design.md:668`（`按R01–R09、E01–E07`）三处 R09 集合口径与 memory 移除后的新结论冲突，必须一并收窄为 `R01–R07`（:514 去掉「以及显式启用的 R09」）：
- [x] 10.4.2 **跨 change 同步项实际执行（本 change 直接改 `add-context-injection-lifecycle`，属被允许的跨 change 同步）**：已在 `openspec/changes/add-context-injection-lifecycle/tasks.md:96`、`design.md:514`、`design.md:668` 三处最小改动（只加约束与具名引用，不重写整体设计），行号以改动后实测为准，登记如下——`tasks.md:96`（6.7 覆盖矩阵 `R01–R09` → `R01–R07`，并加注 R09 已随 `remove-agent-memory` 移除）、`design.md:514`（`R01–R07 以及显式启用的 R09` → `R01–R07`，并加注 R09 已移除）、`design.md:668`（`按R01–R09、E01–E07` → `按R01–R07、E01–E07`，并加注 R09 已移除）。**如实登记：本 change 内不得改其它 change 的正文主线，只做这类最小同步。**
- [x] 10.5 登记 `add-itemized-rollout-context` 的 memory 枚举清理：`design.md:195/:475`、`specs/itemized-rollout-context/spec.md:811/:821/:1118/:1132/:1246`、`specs/checkpoint-history-loading/spec.md:203`、`tasks.md:37/:167`。**行号修正（M3）**：`checkpoint-history-loading/spec.md` 的 memory-provider lookup 场景当前在 `:203`（`spec.md:260` 行号已因提交 `b65f15b1` 移位失效）；本 change 内所有行号引用已逐条复核，`:260` 是唯一失准处。
- [x] 10.5.1 **代码侧 R09 已随 5.1 物理下线（实施轮）**：`app/agents/instruction_producers.py` 的 `InstructionProducerId`（原 `:35`）与 `_KNOWN_PRODUCER_IDS`（原 `:311`）含 `"R09"`，与 policy key `agent_memory`（原 `:43`/`:319`）同属被移除能力的两处登记，现已一并删除；`rg -n "R09" app` 零命中。
- [x] 10.6 登记待裁定项（design.md D7）：`docs/middleware-prompt-vscode-comparison.html:173-181` 的 `MemoryMiddleware` 整卡、`agent.knowledge.retrieval` 4 键、`gateway_snapshot` source kind、`enable_workspace_memory` flag、`PromptTrustLevel.untrusted_reference` 回收。**历史 workspace 重绑流程已按 owner 裁定升级为本 change 的必做迁移任务（见 7.3.1–7.3.3），不再列为待裁定项。**

## 11. OpenSpec 校验与交付

- [ ] 11.1 `/home/hyf/.bun/bin/openspec validate remove-agent-memory --strict`（贴原始输出）。
- [ ] 11.2 `/home/hyf/.bun/bin/openspec validate --strict --all` 必须 0 failed（含本 change 后的基线为 39 passed / 0 failed）。
- [ ] 11.3 提交遵循独立索引纪律：`GIT_INDEX_FILE=/tmp/<任务名>.idx git read-tree HEAD` → `GIT_INDEX_FILE=/tmp/<任务名>.idx git add <精确路径>` → `git commit -m "中文" -- <精确路径>`；禁 `--amend`、禁裸 `git add -A`、禁无路径 commit。
- [ ] 11.4 提交后自检：`git show --name-status <hash>`、`git merge-base --is-ancestor <hash> HEAD`、`git merge-base --is-ancestor <提交前HEAD> HEAD`。
