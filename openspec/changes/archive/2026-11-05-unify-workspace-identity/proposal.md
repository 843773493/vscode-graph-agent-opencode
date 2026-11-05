## Why

当前 Gateway 用 `gw_...` 标识工作区路由，Backend 和资源 URI 则使用工作区自身 UUID，同一工作区在注册、代理、联邦投影和恢复中需要维护两套身份及映射。统一为工作区自身 UUID 作为唯一 `workspace_id`，消除路由变化对工作区身份的影响，并减少后续快速访问、扩展管理及多工作区功能的重复转换。

## What Changes

- **BREAKING** 本地注册、Backend mount、HTTP、资源 URI、协议 DTO、持久引用及客户端恢复统一使用 `.boxteam/workspace-identity.json` 中既有的标准 UUID，字段统一命名为 `workspace_id`；保留已有合法 UUID，不重新生成。
- **BREAKING** 删除 Gateway 的 `gw_...` 工作区别名、`backend_workspace_id`、重复的 `remote_workspace_id` 和本地／远程工作区 ID 换算链；远程投影保留原工作区 UUID，以 `(gateway_id, workspace_id)` 表达资源归属，不再生成本地投影工作区 ID。
- 连接使用既有 `connection_id`；Backend 管理继续保留 `owner_id`、`generation` 及其租约。它们只管理连接或进程，不得再充当工作区身份；不得把原工作区别名简单改名为 `target_id` 后保留同一映射。
- 共享 Backend 成员、生命周期影响集合、root binding 和配置 fan-out 使用相同工作区 UUID。明确目标 Gateway 的代理请求携带独立 Gateway 归属，不复用发送方身份头表达目标。
- 对现有控制面记录提供显式、可恢复的一次性身份迁移；浏览器恢复按准确归属升级或明确拒绝无法解析的旧项，不依赖当前激活工作区，不清空业务历史或建立运行时双读兼容链。
- 保持工作区内存储布局、Session／Thread 身份、唯一原生 Gateway 根绑定、管理授权及跨进程独占行为。切换 `BOXTEAM_HOME` 不能绕过同一物理工作区的现有运行时锁；不新增工作区永久绑定某个 home 或 Backend 的规则。

## Capabilities

### New Capabilities

- `workspace-identity`: 既有工作区 UUID 在控制面、业务面和客户端中的唯一身份合同、明确 Gateway 归属、完整迁移及真实入口验收。复用现有身份 owner，不创建第二个 UUID 注册表。

### Modified Capabilities

- `remote-gateway-federation`: 联邦投影、目录与生成器引用直接保留来源 UUID，移除本地投影别名，并将连接身份与工作区身份分离。
- `managed-backend-lifecycle`: 共享 Backend 成员及根绑定统一使用工作区 UUID，保留原进程所有权、generation 和完整组生命周期。
- `configuration-domains`: `connection_id` 仅表示配置连接元素，registry 中的工作区按统一 UUID 和所属 Gateway 定位，不再混用两者。

## Impact

- Backend：`app/core/workspace_identity.py`、mount／container、工作区所有权锁、配置与历史 owner；不改变业务数据物理归属。
- Gateway：`workspace_ids.py`、registry／SQLite、root binding、代理、联邦 manifest／projection、导航目录、生命周期与配置对账。
- 协议与客户端：proto／descriptor 生成映射、公开 DTO、OpenAPI、Python SDK、Web／Workbench／Electron 共用的 owner、请求目标、搜索 scope、编辑器 serializer 和缓存键；沿同一生成链迁齐。
- 存量：Gateway 注册及 metadata、共享组成员、活动选择、目录／Generator 引用、用户视图态与客户端恢复引用。只迁移确实使用旧工作区别名的字段；canonical 正文及有效资源 URI 不重写。
- 关系：在 `add-multi-workspace-backend-mounting` 已完成实现上推进，取代其中 Gateway target ID 与 Backend UUID 配对的身份条文；保留原验收记录。统一 URI grammar、根绑定授权和进程生命周期仍由原 owner 维护。快速访问和目标感知扩展管理消费本变更，不再新增旧映射调用方。
- 按纵向切片推进实现与验证；存量迁移通过显式维护入口执行，不在普通启动中隐式改变身份。
