## 1. 契约 v2 冻结与单一定义点（本 change 是唯一 owner）

- [ ] 1.1 建立**术语表唯一处**，逐字登记契约 v2：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；显式禁用同义异名（virtual url / VURI / 虚拟 URL）；声明两个并行 change 只能引用此处定义。
- [ ] 1.2 建立**scope_id 唯一表**（唯一处）：登记每个 scope 的 scope_id 取值来源（初审：`workspace`→workspace_id、`gateway`→`local`、`user`→`local`、`inline`→distribution_id；`memory` 待定），并写明「scope_id 对所有 scope 都必填」与「不得自行发明 scope 名或 scope_id 语义」。**最终以 owner 权威表为准。**
- [ ] 1.3 建立**拒绝码集中登记处（唯一处）**：收拢既有 grammar 拒绝码命名空间与风格，登记全部拒绝码（含跨 gateway 码），并写明「具体名称与最少数量以 owner 权威表为准；权威表下发前不得自造或定稿新码名；其它 change/模块只能引用」以及拒绝码闭合的可机械检查方式。
- [ ] 1.4 固定 **v2 统一 VRN 语法**与规范化契约：`boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...canonical path segments}`——`{gateway_authority?}` 为可选单段且承载稳定 gateway_id；`{scope_id}` **对所有 scope 都必填**；`resources` 为**固定保留段**；闭合 charset 与闭合 kind 集（初审在既有基础上新增 `config`）；拒绝 `%` 编码与 `#fragment`；大小写/分隔符/相对段规范化**只有单一实现**。声明现有 skill 形态是本语法特例，且不存在第二套并列语法。
- [ ] 1.5 标注**权威表依赖边界**：在 spec 与 tasks 中显式声明「scope 名 / scope_id 语义 / 拒绝码名称与数量 / `memory` 归属」四类为初审状态，权威表下发前不得据此实现；`memory` 侧重申明「本轮不重新定义、不做设计、不当文件 locator、不定 scope_id」。

## 2. 三层分离与不变量

- [ ] 2.1 定义三层职责的类型与持久化边界：`ResourceIdentity`（不透明、稳定、revision-free、不依赖激活工作区、持久化）、`VRN`（可解析、持久化、允许悬空、禁 revision/hash）、`real path`（机器本地、临时、永不持久化、永不进模型可见载荷、永不跨 gateway 边界，仅作调用栈局部变量）。
- [ ] 2.2 落实 **real path 不变量**的可机械检查：real path 出现在 API 响应体 / 持久化记录 / 模型可见载荷中即为缺陷；给出检查点（响应序列化前、记录落盘前、模型载荷组装前）与显式失败行为，禁止用脱敏或截断静默掩盖。
- [ ] 2.3 落实 **identity 独立于 VRN**：同逻辑名跨两个不同 scope（如 `user` 与某个 `workspace`）为两个不同 identity；跨来源等价/覆盖是独立 concern，不进入 identity 或 VRN 语义；给出「覆盖只影响后续解析与 catalog 快照、既有 identity 与已封存绑定稳定」的验证点。
- [ ] 2.4 落实 **「locator 是输入，不是输出」**不变量：解析命中（含跨 gateway）只返回稳定身份与内容，返回值 MUST NOT 携带 locator；给出可机械检查点。

## 3. scope、scope_id 与 gateway authority

- [ ] 3.1 收敛 scope 闭合集（初审：`workspace` | `user` | `gateway` | `inline` | `memory`），实现 `scope_id` 段**对所有 scope 都必填**，把「依赖当前激活工作区 / 当前 gateway / 当前发行版补齐 scope_id」判为失败。
- [ ] 3.2 实施 `builtin` → `inline` 正名，向 config 域既有词汇（`app/schemas/internal_v2/config.py:20`）收敛；**删除** `app/agents/skill_runtime.py:538` 的 `bundled`→`builtin` 改名 shim；不保留 `builtin` 运行时别名。`user` 作为新增 scope（scope_id=`local`，已拍板）。
- [ ] 3.3 实现「其它工作区 = `workspace` scope + 另一个 `workspace_id`」且不新增 scope；实现可选 gateway authority 段并固定三种含义：缺省 = 本机 gateway、== 本机 gateway_id = 等价缺省、== 对端 gateway_id = 跨 gateway；且 authority MUST NOT 承载瞬时通道标识（channel instance/epoch/route）。
- [ ] 3.4 承认多工作区前置条件：一个后端进程可挂载多个工作区，scope_id 身份在寻址层（HTTP API 与 VRN）显式；持久化数据不绑定「当前激活工作区」。实现细节由并行 change 承载，本 change 只提供寻址层要求与验证点。

## 4. 星型 gateway 解析链

- [ ] 4.1 实现唯一解析顺序：本地 parse（fail-closed）→ 无 authority 或等价本机时由本进程按 workspace registry 解析 → authority 指向对端时按联邦关系转发（本地是 hub 可直接解析直接 spoke；本地是 spoke 经唯一 hub 做一次有界 transit）→ 不可达/未共享/未找到 fail-closed 返回结构化拒绝码。禁止回退本地猜测路径、空路径或虚假默认值。
- [ ] 4.2 把上界做成**显式策略常量**（集中定义，非散落魔法数字）：`visited set`、`max_transit_gateways=1`、`max_gateway_hops=2`、总 deadline；声明「拓扑变化时改策略而非重写解析器」，并覆盖超上界/超 deadline 的显式失败。
- [ ] 4.3 固定跨边界传输契约：只传 identity、VRN、revision 与资源内容；绝不传 real path、provider locator 或 credential；对应「locator 不是输出」不变量。
- [ ] 4.4 实现集中登记的跨 gateway 拒绝码的失败语义（名称以权威表为准），并保证「未授权存在」与「不存在」返回同一结果、不泄露 locator；未知 gateway 不按名称猜测路由。

## 5. 默认寻址政策、语法收敛与配置来源迁移

- [ ] 5.1 把 v2 语法落到 `app/services/infrastructure/resource_platform/virtual_resources/`，替换旧资源形态；在**同一原子步骤**内修正 `app/agents/skill_runtime.py:52`（`boxteam://workspace/agents`）与 `app/agents/skill_runtime.py:619`（`boxteam://workspace/{id}/resources/skills/catalog`）两处绕过 owner 的裸拼接，避免删定义与修消费方之间出现悬挂中间态。
- [ ] 5.2 使解析链接入生产链路：Skill、配置、状态等引用默认以 VRN 解析与传递，real path 只在最后访问点出现；新增持久化字段若需定位资源一律用 `identity + VRN(+ 独立 revision 字段)`，禁止存 real path。
- [ ] 5.3 删除旧形态与其解析实现：不提供别名、双读或兼容层；移除解析链路中「仅打印地址」的半接入状态，确保解析/授权侧有真实生产调用。
- [ ] 5.4 按 D7 迁移**已确证的 real path 持久化违约**：把 `app/services/infrastructure/config/state.py:473` 的 `ConfigSourceLayerRecord` 中 `source_path`/`backup_path` 换成 VRN，**直接复用 config 侧既有平级属性模式**（对齐 `app/core/config_sources.py:16` 的 `path` + `layer` + `precedence` 与 `layer_revision`/`layer_digest`/`source_generation` 兄弟字段），即「`path`→`vrn`，其余 sibling 原样保留」；不另发明第二套结构。
- [ ] 5.5 依据待验证项结果确定迁移形态：先确认「VRN 是否已落进持久化的 session/catalog/checkpoint 数据」与「`ConfigSource.path` 是否经间接路径进 API 响应体」，再判定是「新写字段」还是「真数据迁移」；未确证前不断言迁移细节。

## 6. 与在途 change 的收口（消除两套定义并存）

- [ ] 6.1 更新未归档的 `openspec/changes/add-context-injection-lifecycle/tasks.md`：把 `3.14`（解析器本体）标注为「由本 change 的语法/scope/scope_id/拒绝码定义取代」，把未完成的接线任务 `6.6` 与 `7.1` **指向本 change**（引用而非复制定义）。
- [ ] 6.2 更新该 change 的 `specs/context-injection-lifecycle/spec.md` 中 VRN resolver requirement：显式声明「VRN 语法、scope 闭合集、scope_id 语义与拒绝码以 `add-unified-virtual-resource-addressing` 为准」，并移除与之冲突的第二份语法描述，保留其自身的行为要求（隐藏 locator、activation snapshot 恢复、`skill_load` name-only 等）。
- [ ] 6.3 与两个并行 change 对表：会话上下文 URI 统一改造复用本 change 的 scope/语法归属声明；单后端多工作区挂载复用显式 scope_id 身份要求；三方术语、scope 名、scope_id 语义与拒绝码写法一致，无同义异名。

## 7. 验证与收口

- [ ] 7.1 增加语法与规范化测试：`resources` 固定段不可省略、`scope_id` 对所有 scope 必填、闭合 charset/kind、拒绝 `%` 与 `#fragment`、大小写与分隔符规范化单点、skill 形态作为特例、`inline` 取代 `builtin`（且无运行时别名）、未知 scope/kind 拒绝码正确。
- [ ] 7.2 增加三层分离与不变量测试：real path 外泄即缺陷（响应体/持久化/模型载荷三处检查点）、VRN 悬空合法、revision 不进 VRN、identity 不承担寻址、同名跨 scope 是两个 identity、locator 不在解析输出中。
- [ ] 7.3 增加星型解析与失败语义测试：authority 缺省与本机 gateway_id 同解、hub 直连直接 spoke、spoke 经唯一 hub 一次有界 transit、上界与 deadline 为 policy 常量且超限显式失败、不可达/未共享/未知 gateway 分别返回集中登记的拒绝码、「未授权存在」与「不存在」不可区分、不递归转发。
- [ ] 7.4 增加配置来源迁移测试：`ConfigSourceLayerRecord` 不再持久化 real path、改为 VRN 兄弟字段；`path`→`vrn` 替换后 sibling 字段（layer/precedence/revision/digest/generation）语义与值不变。
- [ ] 7.5 增加多工作区寻址测试：单进程挂载多工作区时各 VRN 在 `scope_id` 显式携带 workspace_id，解析结果不随「当前激活工作区」切换而改变；持久化记录含义与激活态无关。
- [ ] 7.6 运行仓库既有校验并记录证据：`openspec validate add-unified-virtual-resource-addressing --strict` 必须 0 failed，且本 change 与 `add-context-injection-lifecycle` 两侧均无残留的第二套 VRN 语法或自造拒绝码。
