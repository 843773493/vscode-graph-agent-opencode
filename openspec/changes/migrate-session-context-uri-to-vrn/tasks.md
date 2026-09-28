## 1. 依赖与前置对齐

- [ ] 1.1 确认「统一虚拟资源寻址」change 已登记统一 scope 闭集、VRN grammar 与拒绝码命名空间；未登记前不得进入第 3 组实施。
- [ ] 1.2 等待并采用「统一虚拟资源寻址」change 随后下发的**权威表**（scope 名、scope_id 语义、拒绝码）；在其到达前不得定稿这三类内容，接口一律 fail-closed。
- [ ] 1.2-A 与其对齐段序与闭集落地：保留 `resources` 固定段、scope 闭集扩为 `{workspace, user, gateway, inline, memory}`（`builtin` 正名为 `inline`）；本 change 不自行改动 grammar。
- [ ] 1.2-B 冻结 `memory`：不基于它做设计、不为其定 scope_id，等权威表。
- [ ] 1.3 与 itemized rollout context 对齐 `assembly_ref` 的表示（资源身份或专用 ref 类型），确认不改变本 change 的结构化方向。

## 2. 会话上下文资源引用的结构化模型

- [ ] 2.1 定义结构化会话上下文引用模型：`resource_identity`（不透明、稳定、revision-free、不依赖激活工作区）、`vrn`（位置、禁止编码 revision/hash）、`scope`（取自权威闭集，与 vrn 内 scope 一致可校验）、`revision`、`view`、`cursor` 作为彼此并列的字段。
- [ ] 2.2 逐条落地三层分离 / three-layer separation 不变量校验：identity 不含 revision 且不随激活工作区变化；VRN 字符串不含 revision/hash/snapshot ref；real path 不出现在 API 响应体、持久化记录与模型可见载荷中。
- [ ] 2.3 落地 scope 闭集与**必填 scope_id** 校验：拒绝未登记 scope；拒绝**任意 scope** 缺 scope_id；拒绝回退到隐含上下文补全；断言 `resources` 固定段存在且段序不被简化。
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

## 5. 破坏性迁移与失效判定

- [ ] 5.1 让入口停止接受旧式上下文 URI：对含 `%` 编码、`#fragment` 或未登记 scope 自有正则语法的字符串显式拒绝，并在错误信息中指向结构化字段表示。
- [ ] 5.2 扫描既有持久化记录中内嵌的旧式上下文 URI（cursor、审计、引用字段），逐条判定可规范化 / 不可规范化。
- [ ] 5.3 迁移可规范化记录：提取资源身份与位置 → 生成带必填 scope_id 的规范 VRN → revision/视图/游标落到结构化字段 → 保留来源 lineage。
- [ ] 5.4 失效不可规范化记录：显式标记失效并记录原因，禁止静默丢弃或猜测解释。
- [ ] 5.5 落地回滚边界：全部记录迁移成功并校验通过前保留原始记录；任一步失败即停止回滚，系统行为与迁移前等价。
- [ ] 5.6 移除旧的会话上下文自有正则与 `#selector` 解析实现，物理下线，不留兼容层或别名。

## 5A. 配置来源真实路径持久化的迁移（已确证义务）

- [ ] 5A.1 把 `app/core/config_sources.py` 的 `ConfigSource.path: Path` 换成 `vrn: VRN`，兄弟字段（`layer`/`precedence`/`loaded`/`source_key`/`presence`/`layer_revision`/`layer_digest`/`source_generation`）原样保留；不得另发明一套结构。
- [ ] 5A.2 移除 `app/services/infrastructure/config/state.py` 的 `ConfigSourceLayerRecord.source_path`/`backup_path` 持久化，改为 VRN 表达。
- [ ] 5A.3 移除 API 响应体对外输出真实路径（`app/schemas/internal_v2/config.py` 的 `ConfigSourceDTO.path`），改为 VRN；同步更新前端消费点。
- [ ] 5A.4 落地时删除 `app/agents/skill_runtime.py:538` 的 bundled 到 builtin 改名映射，向 `inline` 收敛。
- [ ] 5A.5 断言配置来源的持久化记录与 API 响应体均不含真实路径。

## 6. 收口在途 change

- [ ] 6.1 更新 `openspec/changes/add-itemized-rollout-context/specs/itemized-rollout-context/spec.md` 的会话上下文 URI requirement（约 262/278/288 行）：声明语法与解析以本 change 为准，删除本地自有的 URI 形态定义。
- [ ] 6.2 更新同 change 的 `design.md`（约 890-967 行）与 `tasks.md` 第 8.10 项，使其会话上下文寻址描述引用本 change，不再并列定义第二套 URI 语法。
- [ ] 6.3 运行 `openspec validate add-itemized-rollout-context --strict` 与 `openspec validate migrate-session-context-uri-to-vrn --strict`，确认两 change 均通过且不存在互相矛盾的 URI 定义。

## 7. 命名与校验收口

- [ ] 7.1 全仓校验命名一致性：只允许 `资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；禁止 virtual url / VURI 等同义异名。
- [ ] 7.2 确认本 change 未自造拒绝码、未改动 VRN 语法本体与固定段序；所有新增拒绝场景均引用「统一虚拟资源寻址」change 的登记结果。
- [ ] 7.4 核对待验证项（VRN 是否已落进持久化 session/catalog/checkpoint 数据、`ConfigSource.path` 间接泄漏路径、`inline`/`sqlite` 两层可解析载体、`memory` 形态归属）已查清后再定稿相关 spec 小节；在此之前不得据其断言。
- [ ] 7.3 运行 `openspec validate migrate-session-context-uri-to-vrn --strict`，要求 0 failed。
