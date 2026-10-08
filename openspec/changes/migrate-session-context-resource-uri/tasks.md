> 台账口径说明：本 change 混合登记规范定稿与生产验收，`[x]` 的含义以每项明示证据为准；规范定稿不等于生产完成。配置来源 URI/layer 和本地会话结构化解析已有集成与具名证据，A01 又集成 activation/detail 接线；不再沿用“生产尚未开工”的历史总判定。第 3、4、5、9 组未勾项仍分别承担真实入口、联邦、持久字段与 send/read/wait 的完整验收。

## 1. 依赖与前置对齐

- [x] 1.1 确认「统一资源寻址」change 已登记 scope 闭集（`workspace`/`user`/`gateway`/`inline`；`memory` 已移出）、URI grammar、`kind` 闭集与拒绝码命名空间；未登记前不得进入第 3 组实施。 **（原四kind/grammar/resolve owner生产门已闭合；新增文件child合同仍由统一寻址第8组实施，不据此勾新增范围。）**
- [x] 1.2 按 `add-unified-virtual-resource-addressing` 的 scope 闭集/`scope_id` 唯一表与 kind 闭集**具名引用**落地：scope 闭集 `workspace`/`user`/`gateway`/`inline`、`scope_id` 由真实身份推导（取值规则一律引用该 change，本 change 不复述）、保留 `resources` 固定段、`scope_id` 对所有 scope 必填、`memory` 非 URI 且入口拒绝、会话上下文 kind=`session` / config 来源 kind=`config`；`builtin`→`inline`（含 layer `bundled`→`inline`）改名归该 owner change。**段序、scope 名、scope_id 语义、kind 与拒绝码定义均归该 owner change，本 change 只引用、MUST NOT 复述或另立。** **（规范层已定稿：spec「scope 必须取自闭合集」与「URI 语法形态统一」正文已改为对 owner requirement 的具名引用，不再复述取值表。）**
- [x] 1.3 与 itemized rollout context 对齐 `assembly_ref` 的表示并确认不改变结构化方向：`assembly_ref` 使用 `ResourceIdentity`，具名引用「统一资源寻址」的 requirement「identity 必须独立于 URI 且不跨 scope 混同」，不另造专用 ref 类型。**（规范裁定已完成；资源身份字段的生产建模、转换与接线仍属未完成实现工作，不由本项勾选表示。）**

## 2. 会话上下文资源引用的结构化模型

- [x] 2.1 定义结构化会话上下文引用模型：`resource_identity`（不透明、稳定、revision-free、不依赖激活工作区）、`uri`（位置、禁止编码 revision/hash）、`scope`（取自权威闭集，与 uri 内 scope 一致可校验）、`revision`、`view`、`cursor` 作为彼此并列的字段。 **（规范层已定稿：模型字段与约束见 design D1；位置/URI 政策引用 owner「默认寻址政策必须以 URI 为默认形式」，本 change 不复述。）**
- [x] 2.2 逐条落地三层分离 / three-layer separation 不变量校验：identity 不含 revision 且不随激活工作区变化；URI 字符串不含 revision/hash/snapshot ref；real path 不出现在 API 响应体、持久化记录与模型可见载荷中。 **（规范层已定稿：三层分离正文引用 owner「三层职责必须严格分离」；会话上下文侧验收见 spec 的同名 requirement 两个 scenario。）**
- [x] 2.3 落地 scope 闭集与**必填 scope_id** 校验：拒绝未登记 scope（含 `memory`）；拒绝**任意 scope** 缺 scope_id；拒绝回退到隐含上下文补全；断言 `resources` 固定段存在且段序不被简化。 **（规范层已定稿：spec「scope 必须取自闭合集」与「URI 语法形态统一」的 scenario 覆盖拒绝未登记 scope、任意 scope 缺 scope_id、`resources` 固定段不可简化。）**
- [x] 2.4 落地 URI 规范化只走单一实现（引用「统一资源寻址」owner），按owner分段规则拒绝身份/领域段 `%` 编码与原始 `#fragment`，不整体拒绝已公布文件child段的合法编码。 **（规范层已定稿：spec「URI 语法形态统一」的两个 scenario「会话身份编码和 fragment 不成为后门」「规范化只走单一实现」。）**

## 3. 视图与修订的结构化迁移

- [x] 3.1 将原 fragment 表达的信息迁移为会话上下文解析与入口的结构化字段：`information` → `view=information`；`record={index}` → `view=records` + `record_index`；`assembly={id}` → `view=assembly` + `assembly_ref: ResourceIdentity`。新入口拒绝 fragment，不保留旧字符串解析兼容分支。**（A04完整调用方迁齐，032真实三节点入口、035直接fixture、038前端合同通过；不替代未实施文件工具。）**
- [x] 3.2 移植既有视图与资源种类兼容性校验规则集，并要求未登记 view 显式失败、已登记但与资源 kind 不兼容的 view 以 owner 已登记的 `unsupported_view` 显式失败，均不得降级为默认视图。**（A04 028/029拒绝边界与032真实入口通过，未知view与unsupported_view保持独立拒绝，不降级。）**
- [x] 3.3 保留可重读修订绑定能力：`expected_revision` 不匹配时返回显式修订变更错误；游标继续绑定资源 + revision + operation，在固定修订上推进分页，并在 resource/revision/operation 不匹配时显式失败。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**
- [x] 3.4 断言 revision 只存在于结构化字段：对全部会话上下文路径做检查，确认生成的 URI 字符串在任何情况下都不含 revision/hash/snapshot 引用。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**

## 4. 星型解析 / star-topology resolution 接入

- [x] 4.1 实现唯一解析顺序：本地 parse（fail-closed）→ 本进程解析 → hub 可直接解析其直接 spoke；spoke 经唯一 hub 做一次有界 transit（携带 visited set、`max_transit_gateways=1`、`max_gateway_hops=2`、总 deadline）。覆盖既有单会话 read、远端工作区/Gateway inventory 与 search 的正例；同一 read grant owner 绑定真实 ResourceIdentity/URI/scope 和查询字段，由目标端既有 owner 执行，不伪造 Session 目标、不回退当前激活工作区。集中验证错误目标类型、未共享、签名/请求字段不匹配及原 deadline/hop 边界；send/wait/reply 不接受上下文清单目标。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**
- [x] 4.2 把 `max_transit_gateways`/`max_gateway_hops`/deadline 等上界实现为**显式策略常量**，禁止散落魔法数字。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**
- [x] 4.3 实现 fail-closed 拒绝：对端不可达、资源未共享、资源未找到一律返回结构化拒绝码；禁止猜测路径、禁止返回虚假默认值。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**
- [x] 4.4 约束跨边界载荷只含资源身份、URI、revision 与内容；断言 real path 不跨 gateway 边界。直接下线唯一 information owner/DTO 的 workspace.root_path、storage_path，迁齐 API/proto/生成类型/前端与测试实际 caller；read information、search session_information 及持久观察输出同源，保身份/名称/诊断状态，不在中继过滤、不把验收缩为仅 envelope，也不造 files URI 或兼容字段。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**
- [x] 4.5 落地「locator 是输入不是输出」不变量：断言解析命中的响应只含稳定身份与内容，不含 real path、provider locator 或任何解析 locator。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**

## 5. 入口破坏与「新写字段」（不是存量数据迁移）

- [x] 5.1 让入口停止接受旧式上下文 URI：对含身份/领域段 `%` 编码、`#fragment`、`memory` 两点式或未登记 scope 自有正则语法的字符串显式拒绝，并在错误信息中指向结构化字段表示。 **（规范层已定稿：spec「旧式上下文 URI 必须被入口拒绝且不保留历史数据兼容」的 scenario「旧式 fragment 形态被拒绝」。）**
- [x] 5.2 按用户已授权的开发数据政策确认不保留旧式 URI 历史兼容，不构造扫描/规范化/失效迁移脚本；协调 proposal、design D5 与 spec「旧式上下文 URI 必须被入口拒绝且不保留历史数据兼容」。**（2026-10-08 修订原“复核零存量”任务：原始取证收据未定位，历史摘要未复核，不计为当前零存量证明；新 writer/reader、恢复和真实入口验收要求保留。固定证据复核 report.json SHA256 `2050bc9f0ccfa1ea2e0b7006c7513196d1a70a9978fa152ebdd523e0bb069f74`。）**
- [x] 5.3 新写字段切换：让确需资源定位的既有持久化挂点按 identity + URI 的新格式**新写入**并在读路径切换——`resource_activation_bindings.display_uri` 与 `context_source_control_states`（来源追踪事实），不为不需定位的状态增加冗余URI列。内部聚合的必填nullable display_uri引用统一寻址owner requirement「内部语义聚合资源不得伪造 URI」，activation读写/serde/fork/detail/projection在同一闭包迁齐；不得保留旧写入形态或兼容读写双轨。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**
- [x] 5.4 移除旧的会话上下文自有正则与 `#selector` 解析实现，物理下线，不留兼容层或别名。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**

## 5A. 配置来源真实路径持久化的迁移（正名出处：`add-unified-virtual-resource-addressing`）

本义务的 normative 正文（config kind、来源 URI 与逻辑 layer/precedence、nullable URI、内部 carrier 与 snapshot 边界、real path 不持久化）MUST 取自 `add-unified-virtual-resource-addressing` 的配置来源 URI、config layer 与 URI 兄弟字段 requirements；本 change 只**具名引用**这些 requirements，MUST NOT 复制其规则或另立第二套 config 迁移规范。以下任务只记录该义务的落地与验收证据。

- [x] 5A.1 按 owner 上述 requirement 落地 `app/core/config_sources.py` 的 `ConfigSource.path: Path` → URI，兄弟字段（`layer`/`precedence`/`loaded`/`source_key`/`presence`/`layer_revision`/`layer_digest`/`source_generation`）原样保留。 **（已落地：`ConfigSource.uri: str | None`，兄弟字段逐字保留；构造点统一经 `config/source_uri.py::inline_config_source_uri`。）**
- [x] 5A.2 按 owner 上述 requirement 移除 `app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord.source_path`/`backup_path` 持久化，改为 URI 表达。 **（已落地：`ConfigSourceLayerRecord.uri`/`ConfigSourceJournalRecord.uri` 取代 `source_path`，`backup_path` 物理删除；workspace 与 gateway 两侧 `config_source_layers`/`config_source_journal` 均以迁移 `DROP COLUMN source_path/backup_path` + `ADD COLUMN uri` 收敛，旧值不迁移。）**
- [x] 5A.3 移除配置 API 的真实路径出口并将公共字段统一为 URI：Workspace/Gateway `ConfigSourceDTO.path` → nullable `uri`，`ConfigSourcesDTO.schema_path` → nullable `schema_uri`；不可寻址来源返回 null。层级、precedence 及其它来源事实保持为平级字段。protobuf 保留旧 path/schema_path 的字段号和名称，新字段使用 optional presence；生成 Python/TypeScript、OpenAPI 与前端公共类型同步，不保留旧 alias。**（C03 已串行集成 `69d77eea`；R11 独立审查无阻塞。主树配置/API 46 项、OpenAPI 10 项、ConfigService 73 项、Gateway 路由快照 2 项通过，协议生成/检查、TypeScript 检查与 Web build 通过。路由集合保持不变；内部 schema_path 仅用于本地读取。证据：`out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/c03-main-validation.json` 与 `behavior_reviewer/artifacts/c03-config-uri-contract-review.md`。本项不包含其它持久化 URI 字段迁移。）**
- [x] 5A.4 按 owner 配置来源 URI requirement 保持来源地址只用于可寻址文件；没有稳定来源文件时不编造 URI。 **（已落地：发行包 inline 来源由 `inline_config_source_uri` 提供 URI，runtime override 来源 `uri=None`；本项只核验地址是否存在，不表示 config layer 的 `runtime_override` 正名或 snapshot 分离已经实现，后两项由 owner tasks 5.8 跟踪。）**
- [x] 5A.5 删除 `app/agents/skill_runtime.py` 的 bundled 到 builtin 改名映射，向 `inline` 收敛（scope 正名由 `add-unified-virtual-resource-addressing` 归口）。 **（已由 `298ef599` 落地：`layer_order` 改为 `(inline,gateway,workspace)`，`scope = {...}[layer]` 改名 shim 物理删除，`layer` 名与 URI scope 名自此逐字一致；`rg -n 'bundled' app/agents/skill_runtime.py` 仅剩合法命名的 `resolve_bundled_skill_groups`。该 shim 已不存在，无需再删。）**
- [x] 5A.6 断言配置来源的持久化记录与 API 响应体均不含真实路径。 **（已落地：`test_workspace_config_source.py::test_persisted_source_records_never_hold_real_path`、`test_config_service.py::test_workspace_config_migrates_mutable_json_layers_to_sqlite` 与 `test_config.py::test_config_sources_endpoint_never_exposes_real_path`。）**

- **5A.7 重复定义已闭合**：当前唯一 `ConfigCandidateApplier` 定义在 `app/services/infrastructure/config/contracts.py`，`config/store.py` 与 `config_service/config_service_common.py` 均从中性合同层导入；原“仍双定义、待删”登记已失效。主线程在固定1c20全仓核仅一个定义，历史C03集成与后续URI317项相关验证保留，不重复重构。


## 6. 收口在途 change

- [x] 6.1 更新 `openspec/changes/add-itemized-rollout-context/specs/itemized-rollout-context/spec.md` 的 requirement「跨 Session 协作必须只面向目标 main thread 且不共享协作状态」：声明语法与解析以本 change 为准，删除本地自有的 URI 形态定义。该收敛已由 `e8e65b97` 落地，本任务只做核验，按 requirement 名定位、不使用裸行号。 **（规范层已核验：该 requirement「跨 Session 协作必须只面向目标 main thread 且不共享协作状态」现逐字声明「会话上下文资源引用的 URI 语法、作用域/scope、网关授权段/gateway authority 与解析 MUST 以 `migrate-session-context-resource-uri` change 为准；本 change MUST NOT 自行定义会话上下文 URI 形态」，与本 change 一致。）**
- [x] 6.2 更新同 change 的 `design.md` 小节「跨 Session 地址与无共享状态协作」与 `tasks.md` 任务 8.10，使其会话上下文寻址描述引用本 change，不再并列定义第二套 URI 语法。该收敛同样已由 `e8e65b97` 落地，按小节名与任务号定位、不使用裸行号。 **（规范层已核验：`design.md` 该小节逐字声明其 URI 形态与解析由本 change 拥有；`tasks.md` 8.10 逐字声明「以 `migrate-session-context-resource-uri` 为准、只消费解析结果、不得自行定义会话上下文 URI 形态」。）**
- [x] 6.3 运行 `openspec validate add-itemized-rollout-context --strict` 与 `openspec validate migrate-session-context-resource-uri --strict`，确认两 change 均通过且不存在互相矛盾的 URI 定义。 **（A04主最终strict --all实际41passed/0failed，包含两change；原raw保留。）**

## 7. 命名与校验收口

- [x] 7.1 全仓校验命名一致性：只允许 `资源身份 / ResourceIdentity`、`资源 URI`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；禁止 virtual url / VURI 等同义异名。 **（规范层已核验：本 change 三个 `.md` 中此类禁用词仅出现在显式禁用条款；实现期对全仓再复核属实施项。）**
- [x] 7.2 确认本 change 未自造拒绝码、未改动 URI 语法本体、固定段序与 `kind` 闭集；所有新增拒绝场景均引用「统一资源寻址」change 的登记结果。 **（规范层已定稿：spec「URI 语法形态统一」正文现只具名引用 owner，不再复述模板与段序。）**
- [x] 7.3 运行 `openspec validate migrate-session-context-resource-uri --strict`，要求 0 failed。 **（A04原授权源码闭包已集成c6c7b7af；具名验收与固定独审见下方收据，不包含未授权目录/文件工具。）**

## 8. 已由 owner 裁定（规范定稿，不代表生产实现完成）

以下决定只收口规范分支；其 `[x]` 不代表生产 resolver 或会话上下文代码已经实现。owner 唯一登记处见 `add-unified-virtual-resource-addressing` 的对应 requirement。

- [x] 8.1 **视图兼容性规则定稿**：未登记 view 显式失败；已识别但与资源 kind 不兼容的 view 以 owner 登记的 resolve 拒绝码 `unsupported_view` 显式失败，二者均不得降级为默认视图。本 change 只引用 owner 的拒绝码，不定义第二套；本地 view/resolve 消费已实现；联邦解析仍未完成，完整入口验收由3.2跟踪。
- [x] 8.2 **`assembly_ref` 使用 `ResourceIdentity`**：复用统一 URI owner 的既有领域定义，不另造专用 ref 类型；本决定收口类型分支，ResourceIdentity已进入本地DTO、工具及assembly查询；完整生产验收由3.1跟踪。
- [x] 8.3 **fragment 信息映射为结构化字段**：`#information` 映射到 `view=information`；`#record={index}` 映射到 `view=records` 与 `record_index`；`#assembly={assembly_id}` 映射到 `view=assembly` 与 `assembly_ref: ResourceIdentity`。映射与禁止 fragment 的规范已定稿于 design D2 与 spec「会话上下文视图选择必须结构化」；生产实现仍由 3.1 跟踪。

## 9. 会话目标入口统一（2026-10-05 补充；生产闭包已接通，联合验收待补）

- [ ] 9.1 让send/read/wait共用已有target-main解析owner；裸Session ID和完整会话URI归一到同一稳定Gateway/workspace/Session/main-thread target，再冻结目标，不因激活工作区或channel重连改投。复用已有main-thread endpoint，不新增同义接口或文件工具目标状态机。
- [ ] 9.2 保留各业务Job、Turn、通信ID selector的原合同，验证三入口对同一目标一致、歧义/不可达显式失败，read/wait不物化runtime；durable acceptance/observation仍由itemized8.10闭合，不因URI规范化完成而勾通信恢复。

9.1/9.2 固定复核：现有生产入口共用 `SessionCommunicationService.resolve_main` 并冻结稳定目标，尚无确认的源码缺陷；既有 C1/C3、selector、拒绝和冷观察各自证据可复用，缺同一目标 send/read/wait 各自 bare/full URI 对照、full URI wait 和切换/重连后目标保持的联合收据。剩余验收归同 Sol A06，不据未勾重建解析 owner。报告：`out/tests/temp/2026/10/04/024121-team-execution/wire_contract_reviewer/artifacts/a04-final-three-task-evidence/report.json`。

A04完整证据入口：`out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/addressing-existing-source-bulk-dispatch/ROOT-full-final-candidate/`；105源码＋11主文档tree8215be15严格重建、两独审findings=[]并串行集成。作者manifest引用39具名attempt，028/029授权/loopback、032真实三节点受控Provider、031/037真实信息页/生命周期、035直接68项、038合同16项及039 Web build actual0。033/034原失败由具名fixture修订闭合，其余未变绿灯按依赖复用，不相加重叠pass；全局strict41/0。外部Provider quota与补充C1端口仍UNMET，原100条Ruff诊断无新增，SSE生成后续阶段未认证失败原子性。5.2已按明确数据政策修订，不代表原历史取证通过；9.1/9.2三入口完整同目标验收保持未勾，不能由A04 read/search通过自动推断。
