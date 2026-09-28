## 1. 契约冻结与术语登记（本 change 是唯一 owner）

- [ ] 1.1 在 `openspec/changes/add-unified-virtual-resource-addressing/` 内建立**术语表唯一处**，逐字登记冻结契约 v1 的对照：`资源身份 / ResourceIdentity`、`虚拟资源地址 / VRN`、`真实路径 / real path`、`作用域 / scope`（`workspace`/`user`/`gateway`）、`网关授权段 / gateway authority`、`三层分离 / three-layer separation`、`拒绝码 / rejection code`、`星型解析 / star-topology resolution`；显式禁用同义异名（virtual url / VURI / 虚拟 URL），并声明两个并行 change（会话上下文 URI 统一改造、单后端多工作区挂载）只能引用此处定义。
- [ ] 1.2 建立**拒绝码集中登记处（唯一处）**：收拢既有 grammar 拒绝码命名空间与风格，登记新增的最少跨 gateway 码 `unknown_gateway` / `remote_unreachable` / `remote_not_shared`，并写明「其它 change/模块只能引用，不能自造同义码」以及拒绝码闭合的可机械检查方式。
- [ ] 1.3 固定统一 VRN 语法与规范化契约：`boxteam://[{gateway_authority}]/{scope}/[{workspace_id}/]{kind}/{...canonical path segments}`，闭合 charset 与闭合 kind 集，拒绝 `%` 编码与 `#fragment`，大小写/分隔符/相对段规范化**只有单一实现**；声明现有 skill 形态（authority 缺省）是本语法特例，且不存在第二套并列语法。

## 2. 三层分离与不变量

- [ ] 2.1 定义三层职责的类型与持久化边界：`ResourceIdentity`（不透明、稳定、revision-free、不依赖激活工作区、持久化）、`VRN`（可解析、持久化、允许悬空、禁 revision/hash）、`real path`（机器本地、临时、永不持久化、永不进模型可见载荷、永不跨 gateway 边界，仅作调用栈局部变量）。
- [ ] 2.2 落实 **real path 不变量**的可机械检查：real path 出现在 API 响应体 / 持久化记录 / 模型可见载荷中即为缺陷；给出检查点（响应序列化前、记录落盘前、模型载荷组装前）与显式失败行为，禁止用脱敏或截断静默掩盖。
- [ ] 2.3 落实 **identity 独立于 VRN**：同逻辑名跨 `user` 与 `workspace` scope 为两个不同 identity；跨来源等价/覆盖是独立 concern，不进入 identity 或 VRN 语义；给出「覆盖只影响后续解析与 catalog 快照、既有 identity 与已封存绑定稳定」的验证点。

## 3. scope 与 gateway authority

- [ ] 3.1 收敛 scope 闭合集为 `workspace` | `user` | `gateway`；`workspace` 路径必须显式携带 `workspace_id`，把「依赖当前激活工作区补齐 workspace_id」判为失败；`user` 指向 `${BOXTEAM_HOME:-~/.boxteams}/`；`gateway` 指向 Gateway 控制面。
- [ ] 3.2 实现「其它工作区 = `workspace` scope + 另一个 `workspace_id`」且不新增 scope；实现可选 gateway authority 段并固定三种含义：缺省 = 本机、== self = 等价本机、== 对端 = 跨 gateway，且缺省与显式 self 同解。
- [ ] 3.3 承认多工作区前置条件：一个后端进程可挂载多个工作区，workspace 身份在寻址层（HTTP API 与 VRN）显式；持久化数据不绑定「当前激活工作区」。实现细节由并行 change 承载，本 change 只提供寻址层要求与验证点。

## 4. 星型 gateway 解析链

- [ ] 4.1 实现唯一解析顺序：本地 parse（fail-closed）→ 无 authority 或等价本机时由本进程按 workspace registry 解析 → authority 指向对端时交 gateway 层解析器转发，由对端本地解析并以 identity/VRN/revision/内容应答 → 不可达/未共享/未找到 fail-closed 返回结构化拒绝码。禁止回退本地猜测路径、空路径或虚假默认值。
- [ ] 4.2 固定跨边界传输契约：只传 identity、VRN、revision 与资源内容；绝不传 real path、provider locator 或 credential。实现星型界限（最多一次中继，不递归转发），与既有联邦拓扑约束对齐。
- [ ] 4.3 实现三个跨 gateway 拒绝码的失败语义，并保证「未授权存在」与「不存在」返回同一结果、不泄露 locator；未知 gateway 不按名称猜测路由。

## 5. 默认寻址政策与语法收敛

- [ ] 5.1 把统一语法落到 `app/services/infrastructure/resource_platform/virtual_resources/`，替换旧资源形态；在**同一原子步骤**内修正 `app/agents/skill_runtime.py:52`（`boxteam://workspace/agents`）与 `app/agents/skill_runtime.py:619`（`boxteam://workspace/{id}/resources/skills/catalog`）两处绕过 owner 的裸拼接，避免删定义与修消费方之间出现悬挂中间态。
- [ ] 5.2 使解析链接入生产链路：Skill、配置、状态等引用默认以 VRN 解析与传递，real path 只在最后访问点出现；新增持久化字段若需定位资源一律用 `identity + VRN(+ 独立 revision 字段)`，禁止存 real path。
- [ ] 5.3 删除旧形态与其解析实现：不提供别名、双读或兼容层；移除解析链路中「仅打印地址」的半接入状态，确保解析/授权侧有真实生产调用。

## 6. 与在途 change 的收口（消除两套定义并存）

- [ ] 6.1 更新未归档的 `openspec/changes/add-context-injection-lifecycle/tasks.md`：把 `3.14`（解析器本体）标注为「由本 change 的语法/scope/拒绝码定义取代」，把未完成的接线任务 `6.6` 与 `7.1` **指向本 change**（引用而非复制定义）。
- [ ] 6.2 更新该 change 的 `specs/context-injection-lifecycle/spec.md` 中 VRN resolver requirement：显式声明「VRN 语法、scope 闭合集与拒绝码以 `add-unified-virtual-resource-addressing` 为准」，并移除与之冲突的第二份语法描述，保留其自身的行为要求（隐藏 locator、activation snapshot 恢复、`skill_load` name-only 等）。
- [ ] 6.3 与两个并行 change 对表：会话上下文 URI 统一改造复用本 change 的 scope/语法归属声明；单后端多工作区挂载复用显式 workspace 身份要求；三方术语与拒绝码写法一致，无同义异名。

## 7. 验证与收口

- [ ] 7.1 增加语法与规范化测试：闭合 charset/kind、拒绝 `%` 与 `#fragment`、大小写与分隔符规范化单点、skill 形态作为特例、未知 scope/kind 拒绝码正确。
- [ ] 7.2 增加三层分离测试：real path 外泄即缺陷（响应体/持久化/模型载荷三处检查点）、VRN 悬空合法、revision 不进 VRN、identity 不承担寻址、同名跨 scope 是两个 identity。
- [ ] 7.3 增加星型解析与失败语义测试：authority 缺省与 self 同解、对端转发只传逻辑事实、不可达/未共享/未知 gateway 分别返回对应拒绝码、「未授权存在」与「不存在」不可区分、不递归转发。
- [ ] 7.4 增加多工作区寻址测试：单进程挂载多工作区时各 VRN 显式携带 workspace_id，解析结果不随「当前激活工作区」切换而改变；持久化记录含义与激活态无关。
- [ ] 7.5 运行仓库既有校验并记录证据：`openspec validate add-unified-virtual-resource-addressing --strict` 必须 0 failed，且在本 change 与 `add-context-injection-lifecycle` 两侧都无残留的第二套 VRN 语法或同义拒绝码。

