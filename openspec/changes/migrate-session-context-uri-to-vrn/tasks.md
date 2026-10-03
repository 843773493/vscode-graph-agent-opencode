> 台账口径说明：本 change 对应的**生产实现尚未开工**，本文件同时登记 (a) **规范层可机械定稿**项（已在 `specs/session-context-resource-addressing/spec.md` 与 `design.md` 写清 MUST 断言，或为对 owner / 在途 change 的具名引用与核验）与 (b) **生产实现**项。凡 `[x]` 均只表示「规范层已定稿 / 已具名引用 / 已核验」，MUST NOT 读作「生产已实现」；其余 `[ ]` 为待实现或需 owner 裁定，逐项注明还差什么。

## 1. 依赖与前置对齐

- [ ] 1.1 确认「统一虚拟资源寻址」change 已登记 scope 闭集（`workspace`/`user`/`gateway`/`inline`；`memory` 已移出）、VRN grammar、`kind` 闭集与拒绝码命名空间；未登记前不得进入第 3 组实施。 **（未勾：owner 侧规范层已登记（见其 spec 的 scope/kind/拒绝码 requirement），但本项是**实施前置门**——在 owner 的 grammar 与拒绝码**代码**落地前不得进入本 change 第 3 组实施；属生产门，非规范层可定稿项。）**
- [x] 1.2 按 `add-unified-virtual-resource-addressing` 的 scope 闭集/`scope_id` 唯一表与 kind 闭集**具名引用**落地：scope 闭集 `workspace`/`user`/`gateway`/`inline`、`scope_id` 由真实身份推导（取值规则一律引用该 change，本 change 不复述）、保留 `resources` 固定段、`scope_id` 对所有 scope 必填、`memory` 非 VRN 且入口拒绝、会话上下文 kind=`session` / config 来源 kind=`config`；`builtin`→`inline`（含 layer `bundled`→`inline`）改名归该 owner change。**段序、scope 名、scope_id 语义、kind 与拒绝码定义均归该 owner change，本 change 只引用、MUST NOT 复述或另立。** **（规范层已定稿：spec「scope 必须取自闭合集」与「VRN 语法形态统一」正文已改为对 owner requirement 的具名引用，不再复述取值表。）**
- [x] 1.3 与 itemized rollout context 对齐 `assembly_ref` 的表示并确认不改变结构化方向：`assembly_ref` 使用 `ResourceIdentity`，具名引用「统一虚拟资源寻址」的 requirement「identity 必须独立于 VRN 且不跨 scope 混同」，不另造专用 ref 类型。**（规范裁定已完成；资源身份字段的生产建模、转换与接线仍属未完成实现工作，不由本项勾选表示。）**

## 2. 会话上下文资源引用的结构化模型

- [x] 2.1 定义结构化会话上下文引用模型：`resource_identity`（不透明、稳定、revision-free、不依赖激活工作区）、`vrn`（位置、禁止编码 revision/hash）、`scope`（取自权威闭集，与 vrn 内 scope 一致可校验）、`revision`、`view`、`cursor` 作为彼此并列的字段。 **（规范层已定稿：模型字段与约束见 design D1；位置/VRN 政策引用 owner「默认寻址政策必须以 VRN 为默认形式」，本 change 不复述。）**
- [x] 2.2 逐条落地三层分离 / three-layer separation 不变量校验：identity 不含 revision 且不随激活工作区变化；VRN 字符串不含 revision/hash/snapshot ref；real path 不出现在 API 响应体、持久化记录与模型可见载荷中。 **（规范层已定稿：三层分离正文引用 owner「三层职责必须严格分离」；会话上下文侧验收见 spec 的同名 requirement 两个 scenario。）**
- [x] 2.3 落地 scope 闭集与**必填 scope_id** 校验：拒绝未登记 scope（含 `memory`）；拒绝**任意 scope** 缺 scope_id；拒绝回退到隐含上下文补全；断言 `resources` 固定段存在且段序不被简化。 **（规范层已定稿：spec「scope 必须取自闭合集」与「VRN 语法形态统一」的 scenario 覆盖拒绝未登记 scope、任意 scope 缺 scope_id、`resources` 固定段不可简化。）**
- [x] 2.4 落地 VRN 规范化只走单一实现（引用「统一虚拟资源寻址」owner），并拒绝 `%` 编码与 `#fragment`。 **（规范层已定稿：spec「VRN 语法形态统一」的两个 scenario「拒绝百分号编码与 fragment」「规范化只走单一实现」。）**

## 3. 视图与修订的结构化迁移

- [ ] 3.1 将原 fragment 表达的信息迁移为会话上下文解析与入口的结构化字段：`information` → `view=information`；`record={index}` → `view=records` + `record_index`；`assembly={id}` → `view=assembly` + `assembly_ref: ResourceIdentity`。新入口拒绝 fragment，不保留旧字符串解析兼容分支。**生产实现未完成；规范裁定已单独登记于 8.3。**
- [ ] 3.2 移植既有视图与资源种类兼容性校验规则集，并要求未登记 view 显式失败、已登记但与资源 kind 不兼容的 view 以 owner 已登记的 `unsupported_view` 显式失败，均不得降级为默认视图。**生产实现未完成；规范裁定已单独登记于 8.1。**
- [ ] 3.3 保留可重读修订绑定能力：`expected_revision` 不匹配时返回显式修订变更错误；游标继续绑定资源 + revision + operation，在固定修订上推进分页，并在 resource/revision/operation 不匹配时显式失败。
- [ ] 3.4 断言 revision 只存在于结构化字段：对全部会话上下文路径做检查，确认生成的 VRN 字符串在任何情况下都不含 revision/hash/snapshot 引用。

## 4. 星型解析 / star-topology resolution 接入

- [ ] 4.1 实现唯一解析顺序：本地 parse（fail-closed）→ 本进程解析 → hub 可直接解析其直接 spoke；spoke 经唯一 hub 做一次有界 transit（携带 visited set、`max_transit_gateways=1`、`max_gateway_hops=2`、总 deadline）。
- [ ] 4.2 把 `max_transit_gateways`/`max_gateway_hops`/deadline 等上界实现为**显式策略常量**，禁止散落魔法数字。
- [ ] 4.3 实现 fail-closed 拒绝：对端不可达、资源未共享、资源未找到一律返回结构化拒绝码；禁止猜测路径、禁止返回虚假默认值。
- [ ] 4.4 约束跨边界载荷只含资源身份、VRN、revision 与内容；断言 real path 不跨 gateway 边界。
- [ ] 4.5 落地「locator 是输入不是输出」不变量：断言解析命中的响应只含稳定身份与内容，不含 real path、provider locator 或任何解析 locator。

## 5. 入口破坏与「新写字段」（不是存量数据迁移）

- [x] 5.1 让入口停止接受旧式上下文 URI：对含 `%` 编码、`#fragment`、`memory` 两点式或未登记 scope 自有正则语法的字符串显式拒绝，并在错误信息中指向结构化字段表示。 **（规范层已定稿：spec「旧式上下文 URI 只能被入口拒绝」的 scenario「旧式 fragment 形态被拒绝」。）**
- [ ] 5.2 记录并复核**零存量**取证基线（见 design D5 与 spec「旧式上下文 URI 只能被入口拒绝」）：旧式上下文 URI 无任何持久化实例，故 MUST NOT 构造扫描/规范化/失效的历史数据迁移脚本。
- [ ] 5.3 新写字段切换：让既有持久化挂点按 identity + VRN 的新格式**新写入**并在读路径切换——`resource_activation_bindings.display_uri`（`resource_activation_schema.py:85`）与 `context_source_control_states`（来源追踪事实），不得保留旧写入形态或兼容读写双轨。
- [ ] 5.4 移除旧的会话上下文自有正则与 `#selector` 解析实现，物理下线，不留兼容层或别名。

## 5A. 配置来源真实路径持久化的迁移（正名出处：`add-unified-virtual-resource-addressing`）

本义务的 normative 正文（config kind、来源 VRN 与逻辑 layer/precedence、nullable VRN、内部 carrier 与 snapshot 边界、real path 不持久化）MUST 取自 `add-unified-virtual-resource-addressing` 的配置来源 VRN、config layer 与 VRN 兄弟字段 requirements；本 change 只**具名引用**这些 requirements，MUST NOT 复制其规则或另立第二套 config 迁移规范。以下任务只记录该义务的落地与验收证据。

- [x] 5A.1 按 owner 上述 requirement 落地 `app/core/config_sources.py` 的 `ConfigSource.path: Path` → VRN，兄弟字段（`layer`/`precedence`/`loaded`/`source_key`/`presence`/`layer_revision`/`layer_digest`/`source_generation`）原样保留。 **（已落地：`ConfigSource.vrn: str | None`，兄弟字段逐字保留；构造点统一经 `config/source_vrn.py::inline_config_source_vrn`。）**
- [x] 5A.2 按 owner 上述 requirement 移除 `app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord.source_path`/`backup_path` 持久化，改为 VRN 表达。 **（已落地：`ConfigSourceLayerRecord.vrn`/`ConfigSourceJournalRecord.vrn` 取代 `source_path`，`backup_path` 物理删除；workspace 与 gateway 两侧 `config_source_layers`/`config_source_journal` 均以迁移 `DROP COLUMN source_path/backup_path` + `ADD COLUMN vrn` 收敛，旧值不迁移。）**
- [x] 5A.3 移除 API 响应体对外输出真实路径（`app/api/config.py:102` 的 `path=str(source.path)` 与 `app/schemas/internal_v2/config.py` 的 `ConfigSourceDTO.path`），改为 VRN；同步更新前端消费点。 **（已落地：`ConfigSourceDTO.path` 与 gateway 侧 `GatewayConfigSourceDTO.path` 此前已由来源 VRN 承载；本轮补齐遗留的 `ConfigSourcesDTO.schema_path` 与 gateway 侧 `GatewayConfigSourcesDTO.schema_path` 两处 real path 出口——`app/api/config.py` 的 `schema_path=str(schema_path)` 与 `app/gateway/main.py` 的 `schema_path=str(config.schema_path)`，值改为 config kind 的来源 VRN（复用唯一定点 `config/source_vrn.py::inline_config_source_vrn`，schema 与 `*_inline.jsonc` 同属发行包内资源，走 `inline` scope）；实测 `GET /api/v1/config/sources` 回 `boxteam://inline/<distribution>/resources/config/workspace_schema`，不再回绝对路径。`rg -n 'schema_path|schemaPath' src/clients/web/src --glob '!src/types/**'` 零命中（前端只消费生成类型、无组件读取该字段），故无需改前端消费点；字段名未改（`schema_path` 与 proto `ConfigSourcesDTO.schema_path`/`GatewayConfigSourcesDTO.schema_path` 同名），未触碰 proto 与 4 个生成目录，属值级非破坏性改动。门槛：`tests/unit/api/test_config.py`+`tests/unit/gateway/test_gateway_config.py`=32 passed；`test_config_service.py`+`test_config_reload_contract.py`=75 passed；`import app.main, app.gateway.main`=IMPORT_OK。）**
- [x] 5A.4 按 owner 配置来源 VRN requirement 保持来源地址只用于可寻址文件；没有稳定来源文件时不编造 VRN。 **（已落地：发行包 inline 来源由 `inline_config_source_vrn` 提供 VRN，runtime override 来源 `vrn=None`；本项只核验地址是否存在，不表示 config layer 的 `runtime_override` 正名或 snapshot 分离已经实现，后两项由 owner tasks 5.8 跟踪。）**
- [x] 5A.5 删除 `app/agents/skill_runtime.py` 的 bundled 到 builtin 改名映射，向 `inline` 收敛（scope 正名由 `add-unified-virtual-resource-addressing` 归口）。 **（已由 `298ef599` 落地：`layer_order` 改为 `(inline,gateway,workspace)`，`scope = {...}[layer]` 改名 shim 物理删除，`layer` 名与 VRN scope 名自此逐字一致；`rg -n 'bundled' app/agents/skill_runtime.py` 仅剩合法命名的 `resolve_bundled_skill_groups`。该 shim 已不存在，无需再删。）**
- [x] 5A.6 断言配置来源的持久化记录与 API 响应体均不含真实路径。 **（已落地：`test_workspace_config_source.py::test_persisted_source_records_never_hold_real_path`、`test_config_service.py::test_workspace_config_migrates_mutable_json_layers_to_sqlite` 与 `test_config.py::test_config_sources_endpoint_never_exposes_real_path`。）**

- **5A.7 登记：`ConfigCandidateApplier` 双定义（只登记不删，2026-10-01 独立 slice）**：`app/services/infrastructure/config/store.py:18` 与 `app/services/infrastructure/config_service/config_service_common.py:20` **字面相同**地各定义一次 `Callable[[ConfigSnapshot, ConfigSnapshot], Awaitable[None]]`。同目录 `config_service/AGENTS.md` 明文要求「不得在 `config_service_common.py` 之外重复定义 `logger`、`ConfigCandidateApplier`」，故此为**违反既有指令的重复定义**。**性质**：本次 config 服务拆分（`out/tests/temp/review_config_split/artifacts/REVIEW_config_service_split.md` §五第 1 条）实测为**改动前既存重复**，拆分未消除也未扩大。**裁定：只登记不删，本轮不收口**；收敛归 5A.x config 来源切片（消除重复定义=共享符号唯一定义在 `common`，另一处改为 import）。

## 6. 收口在途 change

- [x] 6.1 更新 `openspec/changes/add-itemized-rollout-context/specs/itemized-rollout-context/spec.md` 的 requirement「跨 Session 协作必须只面向目标 main thread 且不共享协作状态」：声明语法与解析以本 change 为准，删除本地自有的 URI 形态定义。该收敛已由 `e8e65b97` 落地，本任务只做核验，按 requirement 名定位、不使用裸行号。 **（规范层已核验：该 requirement「跨 Session 协作必须只面向目标 main thread 且不共享协作状态」现逐字声明「会话上下文资源引用的 VRN 语法、作用域/scope、网关授权段/gateway authority 与解析 MUST 以 `migrate-session-context-uri-to-vrn` change 为准；本 change MUST NOT 自行定义会话上下文 URI 形态」，与本 change 一致。）**
- [x] 6.2 更新同 change 的 `design.md` 小节「跨 Session 地址与无共享状态协作」与 `tasks.md` 任务 8.10，使其会话上下文寻址描述引用本 change，不再并列定义第二套 URI 语法。该收敛同样已由 `e8e65b97` 落地，按小节名与任务号定位、不使用裸行号。 **（规范层已核验：`design.md` 该小节逐字声明其 VRN 形态与解析由本 change 拥有；`tasks.md` 8.10 逐字声明「以 `migrate-session-context-uri-to-vrn` 为准、只消费解析结果、不得自行定义会话上下文 URI 形态」。）**
- [ ] 6.3 运行 `openspec validate add-itemized-rollout-context --strict` 与 `openspec validate migrate-session-context-uri-to-vrn --strict`，确认两 change 均通过且不存在互相矛盾的 URI 定义。 **（未勾：这是实现期/归档前的**复核命令**，保持未勾以便在实施与归档时重跑；两 change 的规范层当前均通过 `--strict`，见 7.3。）**

## 7. 命名与校验收口

- [x] 7.1 全仓校验命名一致性：只允许 `资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；禁止 virtual url / VURI 等同义异名。 **（规范层已核验：本 change 三个 `.md` 中此类禁用词仅出现在显式禁用条款；实现期对全仓再复核属实施项。）**
- [x] 7.2 确认本 change 未自造拒绝码、未改动 VRN 语法本体、固定段序与 `kind` 闭集；所有新增拒绝场景均引用「统一虚拟资源寻址」change 的登记结果。 **（规范层已定稿：spec「VRN 语法形态统一」正文现只具名引用 owner，不再复述模板与段序。）**
- [ ] 7.3 运行 `openspec validate migrate-session-context-uri-to-vrn --strict`，要求 0 failed。

## 8. 已由 owner 裁定（规范定稿，不代表生产实现完成）

以下决定只收口规范分支；其 `[x]` 不代表生产 resolver 或会话上下文代码已经实现。owner 唯一登记处见 `add-unified-virtual-resource-addressing` 的对应 requirement。

- [x] 8.1 **视图兼容性规则定稿**：未登记 view 显式失败；已识别但与资源 kind 不兼容的 view 以 owner 登记的 resolve 拒绝码 `unsupported_view` 显式失败，二者均不得降级为默认视图。本 change 只引用 owner 的拒绝码，不定义第二套；owner 的 resolver 实现任务仍未完成。
- [x] 8.2 **`assembly_ref` 使用 `ResourceIdentity`**：复用统一 VRN owner 的既有领域定义，不另造专用 ref 类型；本决定收口类型分支，相关生产建模与接线仍未完成。
- [x] 8.3 **fragment 信息映射为结构化字段**：`#information` 映射到 `view=information`；`#record={index}` 映射到 `view=records` 与 `record_index`；`#assembly={assembly_id}` 映射到 `view=assembly` 与 `assembly_ref: ResourceIdentity`。映射与禁止 fragment 的规范已定稿于 design D2 与 spec「会话上下文视图选择必须结构化」；生产实现仍由 3.1 跟踪。
