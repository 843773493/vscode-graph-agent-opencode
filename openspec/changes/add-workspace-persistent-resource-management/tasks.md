## 1. Domain owner 与工作区级持久记录

- [ ] 1.1 为 Browser、Terminal 等 Agent 资源创建工具显式提供 `retention_scope` 参数并默认设为 `thread`；支持用户或显式工具参数选择 `workspace` 创建，或将已有资源提升为工作区范围，提升前后 `resource_id` 不变。
- [ ] 1.2 将工作区资源权威记录存入该工作区 `.boxteam/` 下的领域 owner 存储，并持久化不可变来源 Thread、当前关联、`unattached_since` 和回收状态；实现关联去重、资源版本和 operation lease 冲突校验。目标工作区 MUST 由显式 `workspace_id` 经 `add-multi-workspace-backend-mounting` 的已挂载工作区注册表解析，MUST NOT 依赖「当前激活工作区」。
- [ ] 1.3 为现有 Session 归属资源增加有界、可恢复的一次性迁移：保留 resource_id、来源 Thread 与外部 identity，按 owner 记录重建关联；无可靠 detach 时间的临时孤儿从迁移提交时开始计时；未知状态显式阻断，不扫盘、不双读写。
- [ ] 1.4 实现 backend 重启后的 owner 核实/恢复；外部资源或结果不可确认时持久暴露 unknown/reconcile_required 状态，不重复创建或盲目重放。

## 2. Thread unload、Session 删除和 owner 操作

- [ ] 2.1 将 Thread runtime generation unload 和 Thread 删除接入 owner 的幂等释放事件；owner 按来源 Thread 找到全部 Thread scope 资源，释放句柄和 lease、自动 detach 并立即停止/核实/delete；Workspace scope 只移除本 Thread 关联。
- [ ] 2.2 将 Session 删除接入各资源 owner：先关闭准入并收敛 Session 来源的全部 Thread scope 资源（含已 detach 的孤儿资源），再允许物理删除；保留 Workspace 记录并移除被删 Session/Thread 的引用。
- [ ] 2.3 在每个 owner 实现独立的 promote、attach、detach、stop、delete 操作；Thread scope 只允许关联来源 Thread，Workspace scope 可关联同工作区有效 Thread；detach 不停止资源，带有效 lease/关联的冲突明确失败。
- [ ] 2.4 删除通用 `ResourceManager` 的 `cleanup_policy`、参数猜测和进程内 stopper 决策；确保外部 stop/delete 的已完成状态只由实际领域 owner 核实。
- [ ] 2.5 实现 Thread scope 零关联资源的 30 分钟孤儿回收：以最后一次 Thread detach 持久化计时，回收前复核 scope/关联/lease，并与 attach/promote 串行化；Workspace scope 无关联时不回收。

## 3. Workspace API 与完整投影

- [ ] 3.1 在工作区后端提供 `/api/v1/resources` 列表/详情及 type、scope、当前关联和来源 Thread 过滤，并支持查询无 Thread 关联的 Workspace 资源与宽限期内临时孤儿。
- [ ] 3.2 提供 owner-routed promote、attach、detach、stop、delete 操作；成功返回完整 owner 投影，失败返回稳定错误/阻断事实，不产生部分状态。
- [ ] 3.3 更新 `SessionResourceProviderRegistry` 为只聚合和路由的 projection；验证 Gateway 只透明代理且不读写工作区 `.boxteam/`。

## 4. 前端工作区资源管理

- [ ] 4.1 在资源类型所属的 UI 区域提供工作区范围列表，并在 30 分钟宽限期内展示无 Thread 关联的临时孤儿及计划回收时间，支持重新关联或持久化。
- [ ] 4.2 在 Session 资源表面展示当前 Thread 关联和持久化快捷操作；区分资源关联 attach/detach 与浏览器/WebSocket/终端客户端传输连接。
- [ ] 4.3 按后端完整结果整体替换成功对象；失败后重新读取 owner 状态并展示明确错误；Terminal 资源归主窗口底部面板。

## 5. 生命周期与恢复验证

- [ ] 5.1 为默认 Thread scope 与显式 Workspace scope 创建、Thread unload 即时 detach/delete、有效 lease、promotion/unload 并发、detach 冲突、Session 删除和 owner 重启增加 owner/API 验证；覆盖孤儿资源在 29:59 重新 attach 取消回收、满 30:00 自动回收、lease 阻止回收、attach/回收竞态和 Workspace scope 永不按期限回收。
- [ ] 5.2 在 `tests/e2e/clients/web/test_basic_chat_tool_loop.py` 这一既有唯一 Web E2E owner 中增加用户持久化 Browser/Terminal 资源、unload 后保留、另一个 Thread attach/detach、Session 删除后仍可从 Workspace UI 访问及明确失败反馈的完整场景。
- [ ] 5.3 用外置 process control 验证 owner 在外部操作结果未知时崩溃和重启；恢复孤儿期限时不得重置计时、重放未知副作用、伪报删除成功或遗留无权威记录的外部资源。

## 6. 持久化引用收紧为规范层义务（只新增约束与引用）

- [ ] 6.1 落成规范层禁止项：工作区持久记录、API 响应体与模型可见载荷只承载 `资源身份 / ResourceIdentity` + `虚拟资源地址 / VRN`（必要时并列 revision 字段），MUST NOT 承载 `真实路径 / real path`；检出即 fail-closed。`作用域 / scope` 闭集、`scope_id` 取值语义、VRN 语法、kind 闭集与 `拒绝码 / rejection code` 一律具名引用 `add-unified-virtual-resource-addressing`，本 change MUST NOT 复述或自造。
- [x] 6.2 消除已确证的 real path 持久化：终端记录去掉真实绝对 `cwd` 与顶层 `workspace_root`；终端工作目录作为 owner 自身运行态字段按规范化边界改存工作区内相对目录（`cwd_relative`）并在 PTY 启动调用栈内由工作区根重推导（`terminalSession.js`、`terminalManager.js`），不为此自造 VRN kind；并对既有旧格式记录做有界、可恢复的一次性迁移（load 时推导相对路径并物理写回，推导失败 fail-closed）。浏览器记录去掉 checkpoint 真实文件路径（`browserSession.js`、`browserStateStore.js`）与下载真实文件路径（`browserStateStore.js`），位置可由 owner 身份确定性推导者 MUST NOT 落盘（裁定 D-A2），产物路径改由 `(browser_id, download_id, filename)` 在调用栈内重推导。除 owner 自身运行态字段外，「凡记录需要表达资源所在位置，该位置 MUST 以 VRN 表达」的义务不削弱。
- [x] 6.3 消除 real path 上浮：`session_resource_mapper.py` 的 `cwd`/`checkpoint` metadata 分别收窄为不含路径的 `cwd_relative` 与显式布尔 `checkpoint_available`；`app/agents/tools/terminal.py` 的 `exec_command` 工具结果与 `/api/terminals` 快照的 `cwd` 改为工作区内相对表达 `cwd_relative`，不得出现绝对路径；截图 `image_path` 改为 `screenshot_id` + `screenshot_url`（既有浏览器管理器只读端点，裁定 D-A3），且该端点对 id 不存在、browser 不存在、文件缺失返回稳定结构化错误、不回吐真实路径；新增负向断言「持久记录、API 响应体与模型可见载荷不含 real path」。

### 6.4 越限项登记（判据式，含二级子目录）

判据沿用仓库既有门槛：目标目录 MUST 满足「直接源码文件 ≤20」「单文件 ≤800 行」「不与两个以上领域职责混合」，任一越界即 MUST 附拆分方案或含文件清单、职责/owner 映射、import graph、行数统计与复核结论的架构审查证据。豁免：生成目录（`src/workspace-services/protocol/generated/**`）由上层 `generated/AGENTS.md` 声明不可手改，不计越限。

- 实测快照（提交 `7fe347dc`）：**本 change 归口的单文件 >800 行**：`src/workspace-services/browser/server/browserSession.js=2515`、`src/workspace-services/browser/client/main.js=1814`、`src/workspace-services/browser/server/backend.js=928`（本 change 6.2 已具名 `browserSession.js`/`browserStateStore.js` 并落地其 real path 收紧，三者均须在实施期按浏览器会话生命周期 / 客户端事件与传输装配 / server 协议与生命周期编排职责拆分）。
- **本 change 归口的目录直接 `.js` >20**：`src/workspace-services/browser/server=23`（browser session 生命周期、frame/flow、input、device profile、resource governor 与各自 `*.test.js` 混居，本 change 的 browser 持久资源 owner 落点即在此目录，须按 session / runtime / resources / 测试下沉使直接源码文件数回到 ≤20）。同一范围内 `src/workspace-services/terminal/server=12`、`src/workspace-services/browser/client=11` 等其余目录均未越限。

- 上述登记只补录越限事实与归口，**本 change 不拆这些文件**；拆分与相应架构审查证据属后续独立实施。
