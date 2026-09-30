## 1. 依赖与前置对齐

- [ ] 1.1 确认「统一虚拟资源寻址」change 已登记 scope 闭集（`workspace`/`user`/`gateway`/`inline`；`memory` 已移出）、VRN grammar、`kind` 闭集与拒绝码命名空间；未登记前不得进入第 3 组实施。
- [ ] 1.2 按 `add-unified-virtual-resource-addressing` 的 scope 闭集/`scope_id` 唯一表与 kind 闭集**具名引用**落地：scope 闭集 `workspace`/`user`/`gateway`/`inline`、`scope_id` 由真实身份推导（取值规则一律引用该 change，本 change 不复述）、保留 `resources` 固定段、`scope_id` 对所有 scope 必填、`memory` 非 VRN 且入口拒绝、会话上下文 kind=`session` / config 来源 kind=`config`；`builtin`→`inline`（含 layer `bundled`→`inline`）改名归该 owner change。**段序、scope 名、scope_id 语义、kind 与拒绝码定义均归该 owner change，本 change 只引用、MUST NOT 复述或另立。**
- [ ] 1.3 与 itemized rollout context 对齐 `assembly_ref` 的表示（资源身份或专用 ref 类型），确认不改变本 change 的结构化方向。

## 2. 会话上下文资源引用的结构化模型

- [ ] 2.1 定义结构化会话上下文引用模型：`resource_identity`（不透明、稳定、revision-free、不依赖激活工作区）、`vrn`（位置、禁止编码 revision/hash）、`scope`（取自权威闭集，与 vrn 内 scope 一致可校验）、`revision`、`view`、`cursor` 作为彼此并列的字段。
- [ ] 2.2 逐条落地三层分离 / three-layer separation 不变量校验：identity 不含 revision 且不随激活工作区变化；VRN 字符串不含 revision/hash/snapshot ref；real path 不出现在 API 响应体、持久化记录与模型可见载荷中。
- [ ] 2.3 落地 scope 闭集与**必填 scope_id** 校验：拒绝未登记 scope（含 `memory`）；拒绝**任意 scope** 缺 scope_id；拒绝回退到隐含上下文补全；断言 `resources` 固定段存在且段序不被简化。
- [ ] 2.4 落地 VRN 规范化只走单一实现（引用「统一虚拟资源寻址」owner），并拒绝 `%` 编码与 `#fragment`。

## 3. 视图与修订的结构化迁移

- [ ] 3.1 建立旧形 fragment 到结构化字段的映射：`#information` → `view=information`；`#record={index}` → `view=records` + `record_index`；`#assembly={id}` → `view=assembly` + `assembly_ref`。
- [ ] 3.2 移植既有视图与资源种类兼容性校验规则集，并新增「未识别 view 取值显式失败、不降级为默认视图」。
- [ ] 3.3 保留可重读修订绑定能力：`expected_revision` 不匹配时返回显式修订变更错误；游标继续绑定资源 + revision + operation，在固定修订上推进分页，并在 resource/revision/operation 不匹配时显式失败。
- [ ] 3.4 断言 revision 只存在于结构化字段：对全部会话上下文路径做检查，确认生成的 VRN 字符串在任何情况下都不含 revision/hash/snapshot 引用。

## 4. 星型解析 / star-topology resolution 接入

- [ ] 4.1 实现唯一解析顺序：本地 parse（fail-closed）→ 本进程解析 → hub 可直接解析其直接 spoke；spoke 经唯一 hub 做一次有界 transit（携带 visited set、`max_transit_gateways=1`、`max_gateway_hops=2`、总 deadline）。
- [ ] 4.2 把 `max_transit_gateways`/`max_gateway_hops`/deadline 等上界实现为**显式策略常量**，禁止散落魔法数字。
- [ ] 4.3 实现 fail-closed 拒绝：对端不可达、资源未共享、资源未找到一律返回结构化拒绝码；禁止猜测路径、禁止返回虚假默认值。
- [ ] 4.4 约束跨边界载荷只含资源身份、VRN、revision 与内容；断言 real path 不跨 gateway 边界。
- [ ] 4.5 落地「locator 是输入不是输出」不变量：断言解析命中的响应只含稳定身份与内容，不含 real path、provider locator 或任何解析 locator。

## 5. 入口破坏与「新写字段」（不是存量数据迁移）

- [ ] 5.1 让入口停止接受旧式上下文 URI：对含 `%` 编码、`#fragment`、`memory` 两点式或未登记 scope 自有正则语法的字符串显式拒绝，并在错误信息中指向结构化字段表示。
- [ ] 5.2 记录并复核**零存量**取证基线（见 design D5 与 spec「旧式上下文 URI 只能被入口拒绝」）：旧式上下文 URI 无任何持久化实例，故 MUST NOT 构造扫描/规范化/失效的历史数据迁移脚本。
- [ ] 5.3 新写字段切换：让既有持久化挂点按 identity + VRN 的新格式**新写入**并在读路径切换——`resource_activation_bindings.display_uri`（`resource_activation_schema.py:85`）与 `context_source_control_states`（来源追踪事实），不得保留旧写入形态或兼容读写双轨。
- [ ] 5.4 移除旧的会话上下文自有正则与 `#selector` 解析实现，物理下线，不留兼容层或别名。

## 5A. 配置来源真实路径持久化的迁移（正名出处：`add-unified-virtual-resource-addressing`）

本义务的 normative 正文（config 侧平级属性模式、config kind、尾段形态、`sqlite` 层不可寻址、real path 不持久化）MUST 取自 `add-unified-virtual-resource-addressing` 的 requirement「既有配置来源持久化必须按同一模式迁移为 VRN 兄弟字段」；本 change 只**具名引用**该 requirement，MUST NOT 复述其正文、MUST NOT 另立第二套 config 迁移规范。以下任务只记录该义务的落地与验收证据。

- [x] 5A.1 按 owner 上述 requirement 落地 `app/core/config_sources.py` 的 `ConfigSource.path: Path` → VRN，兄弟字段（`layer`/`precedence`/`loaded`/`source_key`/`presence`/`layer_revision`/`layer_digest`/`source_generation`）原样保留。 **（已落地：`ConfigSource.vrn: str | None`，兄弟字段逐字保留；构造点统一经 `config/source_vrn.py::inline_config_source_vrn`。）**
- [x] 5A.2 按 owner 上述 requirement 移除 `app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord.source_path`/`backup_path` 持久化，改为 VRN 表达。 **（已落地：`ConfigSourceLayerRecord.vrn`/`ConfigSourceJournalRecord.vrn` 取代 `source_path`，`backup_path` 物理删除；workspace 与 gateway 两侧 `config_source_layers`/`config_source_journal` 均以迁移 `DROP COLUMN source_path/backup_path` + `ADD COLUMN vrn` 收敛，旧值不迁移。）**
- [ ] 5A.3 移除 API 响应体对外输出真实路径（`app/api/config.py:102` 的 `path=str(source.path)` 与 `app/schemas/internal_v2/config.py` 的 `ConfigSourceDTO.path`），改为 VRN；同步更新前端消费点。
- [x] 5A.4 按 owner 上述 requirement 落实 `sqlite` 层不给 VRN（`user`/`user_local`/`workspace` 共享同一 `workspace.sqlite`），`inline` 层有稳定 disk 载体故有 VRN。 **（已落地：`ConfigService._config_source`/`_read_shared_override` 与 `source_vrn.py` 文档改以「共享同一 `workspace.sqlite` 这一边界载体」为不可寻址依据——`user`/`user_local`/`workspace`/`sqlite` 一律 `vrn=None`；`user` scope 本身已由 `f6fc990f` 落地进 VRN 闭集，故该结论与 scope 闭集无关，仅由共享载体决定。）**
- [x] 5A.5 删除 `app/agents/skill_runtime.py` 的 bundled 到 builtin 改名映射，向 `inline` 收敛（scope 正名由 `add-unified-virtual-resource-addressing` 归口）。 **（已由 `298ef599` 落地：`layer_order` 改为 `(inline,gateway,workspace)`，`scope = {...}[layer]` 改名 shim 物理删除，`layer` 名与 VRN scope 名自此逐字一致；`rg -n 'bundled' app/agents/skill_runtime.py` 仅剩合法命名的 `resolve_bundled_skill_groups`。该 shim 已不存在，无需再删。）**
- [x] 5A.6 断言配置来源的持久化记录与 API 响应体均不含真实路径。 **（已落地：`test_workspace_config_source.py::test_persisted_source_records_never_hold_real_path`、`test_source_layer_vrn_round_trips_and_sqlite_layer_is_unaddressable`、`test_config_service.py::test_workspace_config_migrates_mutable_json_layers_to_sqlite` 与 `test_config.py::test_config_sources_endpoint_never_exposes_real_path`。）**

## 6. 收口在途 change

- [ ] 6.1 更新 `openspec/changes/add-itemized-rollout-context/specs/itemized-rollout-context/spec.md` 的 requirement「跨 Session 协作必须只面向目标 main thread 且不共享协作状态」：声明语法与解析以本 change 为准，删除本地自有的 URI 形态定义。该收敛已由 `e8e65b97` 落地，本任务只做核验，按 requirement 名定位、不使用裸行号。
- [ ] 6.2 更新同 change 的 `design.md` 小节「跨 Session 地址与无共享状态协作」与 `tasks.md` 任务 8.10，使其会话上下文寻址描述引用本 change，不再并列定义第二套 URI 语法。该收敛同样已由 `e8e65b97` 落地，按小节名与任务号定位、不使用裸行号。
- [ ] 6.3 运行 `openspec validate add-itemized-rollout-context --strict` 与 `openspec validate migrate-session-context-uri-to-vrn --strict`，确认两 change 均通过且不存在互相矛盾的 URI 定义。

## 7. 命名与校验收口

- [ ] 7.1 全仓校验命名一致性：只允许 `资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；禁止 virtual url / VURI 等同义异名。
- [ ] 7.2 确认本 change 未自造拒绝码、未改动 VRN 语法本体、固定段序与 `kind` 闭集；所有新增拒绝场景均引用「统一虚拟资源寻址」change 的登记结果。
- [ ] 7.3 运行 `openspec validate migrate-session-context-uri-to-vrn --strict`，要求 0 failed。
