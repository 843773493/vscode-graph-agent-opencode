## 1. 契约冻结与单一定义点（本 change 是唯一 owner；权威表已下发，据此定稿）

- [ ] 1.1 建立**术语表唯一处**，逐字登记契约术语：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；显式禁用同义异名（virtual url / VURI / 虚拟 URL）；声明其余四个 change 只能引用此处定义。
- [ ] 1.2 建立**scope 闭集与 scope_id 唯一表**（唯一处，定稿）：scope 闭集 = `workspace` | `user` | `gateway` | `inline`；scope_id 语义 = `workspace`→真实 workspace_id、`gateway`→**真实 gateway_id**、`inline`→**真实 distribution_id**、`user`→`local`（显式声明为单用户本地程序约定）。写明「scope_id 对所有 scope 都必填、MUST 由真实身份推导、MUST NOT 硬编码字面量」与「`memory` 不是 VRN scope」。**`inline` 的 scope_id 来源与编码已按 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」定稿（发行 manifest 的 `distribution` + `version`）；`gateway` 的 scope_id 来源与注入 owner 已按 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」定稿（`identity.json` 的 `load_or_create_gateway_id`，Gateway 侧按请求经 `X-BoxTeam-Gateway-Id` 注入）。本表两行均已定稿，不再是初审或待定。**
- [ ] 1.3 建立**拒绝码集中登记处（唯一处，分两套）**：分别登记 grammar 拒绝码（`grammar.py:22-42` 的 17 个，构造期对未登记 code 抛 `ValueError`，闭集不可扩展）与 resolve 拒绝码（`resolver.py:29-38` 的 6 个），标明各自适用范围与「MUST NOT 混用」；写明「新增码只在此出现一次，其它 change/模块只能引用、不得自造同义码」及拒绝码闭合的可机械检查方式。
- [ ] 1.4 固定**统一 VRN 语法**与规范化契约：`boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`——`{gateway_authority?}` 为可选单段且承载稳定 gateway_id；`{scope_id}` 对所有 scope 必填；`resources` 为固定保留段；闭合 charset、唯一 `/` 分隔符、拒绝 `%`/`#`/`\`/`.`/`..`/空段/控制字符/非 ASCII/大小写变体；声明现有 skill 形态是本语法特例，且不存在第二套并列语法。
- [ ] 1.5 固定**kind 闭集**：`agent-spec` | `skills` | `config` | `session`（`config` 承载配置来源文件本身、`session` 承载会话上下文资源/会话定位，二者均为新增且在此定稿）。明确 `_RESOURCE_KINDS`（语法 kind，`grammar.py:18`）与 `_DESCRIPTOR_KINDS`（描述符 kind，`values.py:24`）是**两个独立闭集，不可混用**；**两者当前取值均为 `agent-spec`/`skills`，描述符闭集原有成员 `memory` 已随提交 32bc6256 物理移除**，语法侧对两点式 `boxteam://memory/...` 以 `unknown_scope` 类拒绝码 fail-closed 拒绝。

## 2. 三层分离与不变量

- [ ] 2.1 定义三层职责的类型与持久化边界：`ResourceIdentity`（不透明、稳定、revision-free、不依赖激活工作区、持久化）、`VRN`（可解析、持久化、允许悬空、禁 revision/hash）、`real path`（机器本地、临时、永不持久化、永不进模型可见载荷、永不跨 gateway 边界，仅作调用栈局部变量）。
- [ ] 2.2 落实 **real path 不变量**的可机械检查：real path 出现在 API 响应体 / 持久化记录 / 模型可见载荷中即为缺陷；给出检查点（响应序列化前、记录落盘前、模型载荷组装前）与显式失败行为，禁止用脱敏或截断静默掩盖。
- [ ] 2.3 落实 **identity 独立于 VRN**：同逻辑名跨两个不同 scope（如 `user` 与某个 `workspace`）为两个不同 identity；跨来源等价/覆盖是独立 concern；给出「覆盖只影响后续解析与 catalog 快照、既有 identity 与已封存绑定稳定」的验证点。
- [ ] 2.4 落实 **「locator 是输入，不是输出」**不变量：解析命中（含跨 gateway）只返回稳定身份与内容，返回值 MUST NOT 携带 locator；给出可机械检查点。

## 3. scope、scope_id 与 gateway authority

- [ ] 3.1 实现定稿 scope 闭集 `workspace` | `user` | `gateway` | `inline`（含 `grammar.py:17` 的 `_SCOPE_KEYWORDS`、`:19` 的 `_SKILL_SCOPES`、`:224` 文档串），实现 `scope_id` 段**对所有 scope 都必填**，把「依赖当前激活工作区 / 当前 gateway / 当前发行版补齐 scope_id」判为失败。
- [ ] 3.2 实施 `builtin` → `inline` 正名（含 layer `bundled` → `inline`），向 config 域既有词汇（`app/schemas/internal_v2/config.py:20`）收敛；**删除** `app/agents/skill_runtime.py:538` 的 `bundled`→`builtin` 改名 shim（两处同步改名后退化为恒等映射）；同步 `:427` 的 `_SKILL_LAYER_LABELS` 与 `:503` 的 layer 闭集；不保留任何运行时别名。同步更新断言 `tests/unit/agents/test_skill_runtime.py` 与 `virtual_resources` 测试中 `scope="builtin"` 的用例，避免零回归红线破裂。**只改真依赖 VRN scope 的位置**：`resolver.py:205`、`grammar.py:17/19/224`；无关同名（工具 origin、主题来源、`builtin_tool_registry`）MUST NOT 误改。
- [ ] 3.3 建立**真实身份来源**并据此推导 scope_id：`gateway`→真实 gateway_id，取 `${BOXTEAM_HOME}/gateway/identity.json` 的 `load_or_create_gateway_id`（`gateway_<32hex>`，按 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」由 Gateway 侧按请求注入并经 `X-BoxTeam-Gateway-Id` 读取，MUST NOT 用 host:port/监听端口、MUST NOT 用进程级单例）；`inline`→真实 distribution_id，来源与编码按 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」从发行包 runtime manifest 的 `distribution` + `version` 推导（当前全仓零装配，属尚未实现而非来源未定）；`user`→`local`（单用户本地程序约定）；把 `skill_runtime.py:539` 的 `else "local"` 拆为 `gateway`→真实 gateway_id、`inline`→distribution_id，并建立 `ResolutionContext` 第一条生产构造链路。
- [ ] 3.4 实现「其它工作区 = `workspace` scope + 另一个 `workspace_id`」且不新增 scope；实现可选 gateway authority 段并固定三种含义：缺省 = 本机 gateway、== 本机 gateway_id = 等价缺省、== 对端 gateway_id = 跨 gateway；且 authority MUST NOT 承载瞬时通道标识（channel instance/epoch/route）。
- [ ] 3.5 承认多工作区前置条件：一个后端进程可挂载多个工作区，scope_id 身份在寻址层（HTTP API 与 VRN）显式；持久化数据不绑定「当前激活工作区」。实现细节由 `add-multi-workspace-backend-mounting` 承载，本 change 只提供寻址层要求与验证点。

## 4. 星型 gateway 解析链

- [ ] 4.1 实现唯一解析顺序：本地 parse（fail-closed）→ 无 authority 或等价本机时由本进程按 workspace registry 解析 → authority 指向对端时按联邦关系转发（本地是 hub 可直接解析直接 spoke；本地是 spoke 经唯一 hub 做一次有界 transit）→ 不可达/未共享/未找到 fail-closed 返回结构化拒绝码。禁止回退本地猜测路径、空路径或虚假默认值。
- [ ] 4.2 把上界做成**显式策略常量**（集中定义，非散落魔法数字）：`visited set`、`max_transit_gateways=1`、`max_gateway_hops=2`、总 deadline；声明「拓扑变化时改策略而非重写解析器」，并覆盖超上界/超 deadline 的显式失败。
- [ ] 4.3 固定跨边界传输契约：只传 identity、VRN、revision 与资源内容；绝不传 real path、provider locator 或 credential；对应「locator 不是输出」不变量。
- [ ] 4.4 实现集中登记的跨 gateway 失败语义（按 resolve 闭集选码），并保证「未授权存在」与「不存在」返回同一结果、不泄露 locator；未知 gateway 不按名称猜测路由。

## 5. 默认寻址政策、语法收敛与配置来源迁移

- [ ] 5.1 把统一语法落到 `app/services/infrastructure/resource_platform/virtual_resources/`，替换旧资源形态；在**同一原子步骤**内修正 `app/agents/skill_runtime.py:52`（`boxteam://workspace/agents`）与 `:619`（`boxteam://workspace/{id}/resources/skills/catalog`）两处绕过 owner 的裸拼接，避免删定义与修消费方之间出现悬挂中间态。
- [ ] 5.2 使解析链接入生产链路：Skill、配置、状态等引用默认以 VRN 解析与传递，real path 只在最后访问点出现；新增持久化字段若需定位资源一律用 `identity + VRN(+ 独立 revision 字段)`，禁止存 real path。
- [ ] 5.3 删除旧形态与其解析实现：不提供别名、双读或兼容层；移除解析链路中「仅打印地址」的半接入状态，确保解析/授权侧有真实生产调用。
- [ ] 5.4 按 D10 迁移**已确证的 real path 持久化违约**：把 `app/services/infrastructure/config/state.py:475` 的 `ConfigSourceLayerRecord.source_path` / `:485` 的 `backup_path` 与 `:632` 的 `ConfigSourceJournalRecord.source_path` 换成 VRN；`app/api/config.py:102` 的 `path=str(source.path)` 改为 VRN，**直接复用 config 侧既有平级属性模式**（对齐 `app/core/config_sources.py:16` 的 `path` + `layer` + `precedence` 与 `layer_revision`/`layer_digest`/`source_generation` 兄弟字段），即「`path`→`vrn`，其余 sibling 原样保留」；不另发明第二套结构。
- [ ] 5.5 落实 **config kind 与 sqlite 层不可寻址**：config 资源用 `kind=config` 标识来源文件本身；`layer` 作为兄弟字段保留（不塞进 VRN）；`inline` 层有稳定 disk 载体故有 VRN；`sqlite` 层因 `user`/`user_local`/`workspace` 共享同一 `workspace.sqlite` MUST NOT 编 VRN，并在代码与注释中显式说明该不可寻址性。
- [ ] 5.6 迁移形态定为**「加列 + 写路径 + 切读路径」**（VRN 已确证零落盘：157 live + 44 dev/temp 库 0 命中）；持久化挂点为 `context_source_control_states`（已存来源事实、无 URI 列）与 `resource_activation_bindings.display_uri`（已建表、生产从不写）。**MUST NOT** 编写扫描/规范化/失效存量 VRN 实例的任务（那是空转）。

## 6. 与其它 change 的收口（消除多套定义并存）

- [ ] 6.1 更新未归档的 `openspec/changes/add-context-injection-lifecycle/tasks.md`：把 `3.14`（解析器本体）标注为「由本 change 的语法/scope/scope_id/kind/拒绝码定义取代」，把未完成的接线任务 `6.6` 与 `7.1` **指向本 change**（引用而非复制定义）。
- [ ] 6.2 更新该 change 的 `specs/context-injection-lifecycle/spec.md` 中 VRN resolver requirement 的归属声明：写明 scope 闭集定稿为 `workspace`/`user`/`gateway`/`inline`、`memory` 已确证不是 VRN scope，语法、scope_id 语义、kind 与拒绝码以本 change 为准；保留其自身行为要求（隐藏 locator、activation snapshot 恢复、`skill_load` name-only 等）。
- [ ] 6.3 与其余三个 change 对表：`migrate-session-context-uri-to-vrn` 复用本 change 的 scope/语法/kind 归属；`add-multi-workspace-backend-mounting` 复用显式 scope_id 身份要求；`add-workspace-persistent-resource-management` 复用持久化引用政策；四方术语、scope 名、scope_id 语义、kind 与拒绝码写法一致，无同义异名。
- [ ] 6.4 收口在途 change 的残留旧词汇与 `memory` 形态（本轮已就地修正，登记为完成基线，实施期不得回退）：`add-context-injection-lifecycle/design.md:240-245` 示意块（`builtin`→`inline`，memory 行已移出）；同文件 `:248`（`memory` 两点式标注为非 VRN 示意）、`add-context-injection-lifecycle/specs/context-injection-lifecycle/spec.md:333`（正文 `builtin`→`inline`、`memory` 注明非 VRN scope）、`add-itemized-rollout-context/specs/session-turn-history/spec.md:134`（`builtin`→`inline`、`memory` 注明非 VRN scope）。
- [ ] 6.5 收口第四个并行 change `add-workspace-persistent-resource-management`：其持久化工作区资源记录只保存稳定 `resource_id`（不保存 real path），与「持久化引用 = identity + VRN」政策不冲突；已在其 `proposal.md` 添加引用本 change 的持久化引用政策声明。实施期须复核其 `resource_id` 到 VRN 的绑定方式。

## 7. 验证与收口

- [ ] 7.1 增加语法与规范化测试：`resources` 固定段不可省略、`scope_id` 对所有 scope 必填、闭合 charset/kind（含新增 `config`）、拒绝 `%` 与 `#fragment`、大小写变体被拒绝（非静默归一）、分隔符/相对段规范化单点、skill 形态作为特例、`inline` 取代 `builtin`（含 layer，且无运行时别名）、未知 scope（含 `memory` 两点式）/kind 拒绝码正确。
- [ ] 7.2 增加三层分离与不变量测试：real path 外泄即缺陷（响应体/持久化/模型载荷三处检查点）、VRN 悬空合法、revision 不进 VRN、identity 不承担寻址、同名跨 scope 是两个 identity、locator 不在解析输出中。
- [ ] 7.3 增加 scope_id 推导测试：`gateway`/`inline` 的 scope_id 不再是字面量 `local` 且两者不再相同；`inline` 使用真实 `distribution_id` 并按 requirement「inline scope 的 scope_id 由 manifest 的 distribution 与 version 定稿推导」的编码规则可逆还原为 `(distribution, version)`、跨安装路径稳定、manifest 缺字段时 fail-closed；`gateway` 按 requirement「gateway scope 的 scope_id 由 Gateway 身份文件按请求注入推导」验证「按请求注入而非进程级单例」「缺失/非法时 fail-closed」「MUST NOT 用 host:port/监听端口」；`user` 恒为 `local`。
- [ ] 7.4 增加星型解析与失败语义测试：authority 缺省与本机 gateway_id 同解、hub 直连直接 spoke、spoke 经唯一 hub 一次有界 transit、上界与 deadline 为 policy 常量且超限显式失败、不可达/未共享/未知 gateway 分别返回集中登记的 resolve 拒绝码、「未授权存在」与「不存在」不可区分、不递归转发。
- [ ] 7.5 增加拒绝码登记测试：grammar 与 resolve 两套闭集各自独立、未登记码构造即失败、两套不混用、其它模块无自造同义码。
- [ ] 7.6 增加配置来源迁移测试：`ConfigSourceLayerRecord`/`ConfigSourceJournalRecord` 不再持久化 real path、改为 VRN 兄弟字段；`path`→`vrn` 替换后 sibling 字段（layer/precedence/revision/digest/generation）语义与值不变；`GET /api/v1/config/sources` 响应体不含真实路径；`sqlite` 层不编 VRN。
- [ ] 7.7 运行仓库既有校验并记录证据：`openspec validate --strict --all` 必须 0 failed，且本 change 与其它四个 change 之间均无残留的第二套 VRN 语法、自造 kind 或自造拒绝码。
