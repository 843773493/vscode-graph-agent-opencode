## 1. 身份合同与迁移范围

- [x] 1.1 全仓检索工作区别名生成、`backend_workspace_id`／`remote_workspace_id` 及其直接和间接消费者，标出真实工作区引用与独立连接／导航身份。包括 camelCase、旧身份头、projection 生成器和不含旧字段名的持久引用；范围见 design 的迁移输入表。
- [x] 1.2 明确支持的 registry、配置及客户端旧版本，确认需迁移的实际表、字段和恢复格式；与多工作区、quick-access、扩展管理规划对齐，避免新增旧映射调用方。输入基线与正确 owner 不改写边界见 design；迁移实现及恢复验收仍在第 5／6 组。
- [ ] 1.3 在现有受控身份入口完成注册前 UUID 获取、已有 UUID 保留及同原生 Gateway 根冲突拒绝，不增加第二身份源或占位 workspace_id。
- [x] 1.3a 身份文件完整写入后原子不覆盖发布，消除并发读取空文件；覆盖创建／绑定竞争、候选失败清理与真实 Gateway 损坏恢复。此子项不代表 1.3 的登记与根冲突验收完成。Windows 实机验证尚未完成：`hyf-win` 目标已有两项本地删除，快照激活拒绝，未覆盖目标改动。

## 2. 原生 Gateway 登记与本地请求链

- [ ] 2.1 将本地工作区登记、唯一 root binding、活动选择和路由键迁为工作区 UUID 与 Gateway 归属，保持 owner／租约／registry revision CAS。
- [ ] 2.2 迁齐本地管理和业务代理的工作区定位，统一路径与头部 UUID，拒绝冲突及错误归属，保留原 request ID 和管理授权。
- [ ] 2.3 同步协议 descriptor／proto、公开 DTO、生成映射与校验、OpenAPI 和 SDK；更新破坏性协议版本，执行生成产物一致性检查和受影响类型检查。
- [ ] 2.4 通过真实注册和 HTTP／URI 文件入口验证各层 UUID 相同、两工作区隔离、相同 UUID 不同根冲突及未授权根管理拒绝。

## 3. 共享 Backend 生命周期

- [ ] 3.1 将 managed Backend 成员、generation 启动合同及 mount 校验迁为单一 UUID，保留唯一原生 Gateway root binding 和唯一进程句柄。
- [ ] 3.2 迁齐 blockers、完整影响集合、force、配置 fan-out、移除、shutdown、preserve/adopt 与恢复，删除组生命周期中的工作区 ID 换算。
- [ ] 3.3 经产品入口验证 sibling blocker、排空超时、显式 force、完整组恢复及辅助服务保留；缺失或错误 binding 必须拒绝部分 adopt。
- [x] 3.4 用受进程外保护的独立 Backend 子进程验证不同 home 争用同一隔离工作区仍失败、释放锁后可接管且 UUID 与业务历史保持不变。Linux 真实 Backend A／B／C 使用三个独立 home，A 由消息入口产生执行历史，B 被物理锁拒绝且 A 继续服务；A 退出后 C 保留 Session／main Thread、完整历史条目与身份文件，并从原检查点续跑，实际规范化模型请求前缀完全相同。矩阵 `integration-workspace-process-exclusivity`：1 passed／64.69s，沿现有 model_stream 精确回放，Ruff／Pyright 通过；不代表跨 Gateway 身份迁移或 Windows 实机验收。

## 4. 联邦与连接身份分离

- [ ] 4.1 将 manifest、projection 和远程代理迁为来源 `(gateway_id, workspace_id)`，移除投影 ID 生成及远程 UUID 换算，保留 cursor、CAS 与 lease。
- [ ] 4.2 迁齐连接配置对账和工作区关联，同一来源多连接只保留一个资源身份，删除连接不误删其他关联；保留 generation 排空和独立凭据边界。
- [ ] 4.3 接通独立目标 Gateway 载体，迁齐远程生命周期、目录和 Generator 引用，不改变发送方身份头、管理授权或有界联邦规则。
- [ ] 4.4 通过两个真实 Gateway 验证同 UUID 不同归属、多连接同来源、连接更新／离线、快照恢复及不兼容协议拒绝，检查请求 UUID 不经换算。

## 5. 客户端引用与持久恢复

- [ ] 5.1 迁齐共享 owner、Gateway 请求、工作区选择、目录／搜索 scope、缓存键及相关客户端运行面，删除路由 ID 到 Backend UUID 的转换函数和调用方。
- [ ] 5.2 迁齐编辑器 serializer、活动选择、导航／Generator 与用户视图态的持久引用；按 Gateway 归属保存同一 UUID。
- [ ] 5.3 接入显式迁移恢复材料，准确升级旧客户端引用；无法确认的项逐项报告并保留可重试信息，不切到当前工作区或清空有效状态。
- [ ] 5.4 运行受影响客户端真实类型检查及 Workbench 构建，以真实 Gateway 和隔离浏览器验证两工作区切换、远程选择、文件打开、搜索及重载恢复。

## 6. 存量迁移与单链路收尾

- [ ] 6.1 实现只读预览与显式可恢复迁移，核验完整映射、owner、引用和未结算状态；仅按实际结构变化提升 schema，离线或歧义时保留原记录并阻塞发布。
- [ ] 6.2 原子迁移 registry／metadata、root binding、managed_backends、活动选择、目录／Generator 和 user_view_state；保留有效 URI、canonical 正文、历史 hash 及 Session／Thread 身份。
- [ ] 6.3 用受支持旧版本完整输入验证成功、重入、冲突、远端离线、写入中断恢复和维护窗口回退，检查迁移后真实重启与客户端恢复，不手工补齐产品应生成的关联。
- [ ] 6.4 迁齐后物理删除旧别名生成器、重复字段、换算接口、死代码和旧协议分支；完成全仓消费者核对、相关静态分析及定向回归，保留准确命令、源码版本与原始结果。所有测试使用既有矩阵或进程外超时／内存保护。

### 2026-11-04：6.1 只读登记身份预检切片

- 新增显式 `python -m configs.boxteam preview-workspace-identity`，只读 registry 13／Gateway SQLite 18／connection record 4；不安装配置、不创建缺失身份、不启动目标。运行时与维护入口复用唯一 registry 快照投影，metadata 与行使用同一读事务，避免并发发布时混合 revision。
- 本地身份经既有目标侧只读入口核验，映射保留登记 Gateway、旧别名、原生 Gateway／UUID 和连接；重复 UUID、错误归属、父链、成员、活动引用及未结算控制状态明确阻塞。远端缓存声明不算验证成功，保留联邦核验 TODO；`apply_available=false`，完整引用迁移、恢复材料和原子发布仍未实现，6.1 不勾选。
- 独立审查发现空库／损坏库的 SQLite 错误会越过 CLI 诊断出口；已显式纳入同一错误出口，并以真实 CLI 验证 JSON 归属、非零退出及原件不变。最新定向 19 passed／8.78s，Ruff 与 Pyright 退出 0；更广 registry／配置回归日志与 XML 为 175 passed／2 skipped，但外层进程句柄丢失，未取得该次精确退出码，不表述为完整退出码验收。原件位于 `out/tests/temp/workspace_identity_preflight_20261104/artifacts/`；本轮收尾指纹、独立审查及严格校验见 `out/tests/temp/2026/11/04/openspec_parallel_01/root/artifacts/`。

### 2026-11-04：6.1 导航持久引用预检切片

- 导航 owner 暴露并复用唯一只读节点读取与图校验，格式 1 之外明确拒绝；普通导航和维护预检不再各自解析。预检不调用会同步保存的 `list_tree`，按原 node ID 输出工作区字段的候选归属，不改 folder、父链、显示名或原文件。未知引用、重复节点、父链循环、错误格式及没有 registry 的孤立引用均阻塞，不以自动删节点掩盖迁移缺件。
- 产品导航 owner 生成的本地及远端引用覆盖同 UUID 不同 Gateway，并验证文件原件不变；已有规范 URI 和业务正文不进入此映射。配置／导航定向 41 passed／8.10s，Ruff、实际项目解释器 Pyright 和严格规范校验退出 0；准确耗时以原始日志为准，原件与源码指纹位于 `out/tests/temp/identity_navigation_preflight_20261104/artifacts/`。本切片不包含 user_view_state、旧 Generator、客户端恢复与可恢复发布，6.1 保持未勾选。

### 2026-11-04：6.1 用户视图持久引用预检切片

- 在原 SQLite 只读事务内读取 `user_view_state` 的完整原主键，与 registry 共用同一快照；仅按登记旧 key 精确关联，保留用户、Session 及所有视图业务字段。不能根据 Backend UUID、显示名或当前活动项猜测归属；未登记引用和缺少 registry 的孤立行明确阻塞。
- 原用户／视图 owner 生成多用户、多 Session、同 UUID 不同 Gateway 及多连接输入，核对原主键、全库逻辑不变与并发发布时的快照一致性。定向 97 passed；Ruff、项目解释器 Pyright、编译均退出 0，源码指纹与准确命令、原始日志、实际退出码位于 `out/tests/temp/identity_view_preflight_sol_20261104/artifacts/verification.json`，集成时指纹一致。
- 本切片未实现 apply、引用写迁移、远端身份核验、旧 Generator、客户端恢复材料或可恢复原子发布，不提升 schema；6.1 保持未勾选。
