# 目录用途

`app/core/` 存放不带具体业务语义的后端通用内核，包括环境读取、路径契约、标识符、事件总线、后台任务注册、Trace 中间件和一次性存储迁移。

## 子包索引

- `session_control_primitives.py`：per-session `session-control.sqlite` 各垂直链路共享的形态原语（当前为 `SHA256_HEX_PATTERN`）。只放跨子包共用的形态约束，不放任何表的 DDL、行投影或读写方法。
- `session_control_thread_catalog/`：thread catalog 与 lifecycle fence 一条垂直链路（main/child 权威指针、生命周期闸门 CAS、已发布 child 的冻结 locator 解析、`thread_catalog` v1→v2 加法升级）。只放这两张表的职责；creation record、execution intent、operation lease、owner binding 与通信账本不放这里。
- `session_control_thread_owner_binding/`：thread owner binding 字段槽一条垂直链路（`thread_owner_bindings` 行投影、canonical JSON 列表槽解析、2.1 负面合同校验、ensure/get/update，以及该表行插入的唯一 SQL 实现）。只放 owner 侧记录槽；prefix epoch 与 ToolSet revision 的权威解释仍属对应 domain owner，不构成第二 writer。
- `session_control_operation_lease/`：通用 operation lease 一条垂直链路（`session_operation_leases` DDL 与非终态索引、行投影、create-or-get 幂等准入、fencing token CAS 链与读取）。只放持久准入/操作 lease；`SessionOperationLease` 的 typed 字段集与状态闭集仍由 `session_lifecycle_gate.py` 单点定义。

# 可修改内容

- 可以维护跨业务模块共享且没有领域流程的基础能力。
- 可以为 `session_control_*` 子包补充目录索引与职责边界说明；新增垂直链路时同步在此登记。
- 可以在 `path_utils.py` 中定义全局目录、工作区目录和会话物理树根目录；权威索引读取、完整性校验及按稳定 ID 查找节点的规则必须集中在正式会话路径解析器中。
- 可以在 `user_storage_migration.py` 中实现配置安装维护入口所需的用户级存储迁移。

# 不可修改内容

- 不要放入会话、消息、Agent、工具或 Gateway 的业务流程。
- 不要让路径初始化隐式创建默认工作区、安装用户配置或修改 Gateway 注册表。
- 不要通过当前文件的 `parent` / `parents` 向上猜测仓库根目录。
- 不要让新的运行时代码继续写入旧目录 `~/.boxteam/` 或已废弃的工作区顶层会话数据目录。

# 规范

- 全局根目录统一为 `${BOXTEAM_HOME:-~/.boxteams}/`，工作区业务根目录统一为 `${workspace_abs_path}/.boxteam/`。
- 路径函数应返回明确的绝对路径；调用方必须显式提供工作区路径或从受控运行时状态取得它。
- 工作区目录初始化只处理当前显式工作区，不产生与该工作区无关的全局副作用。
- 禁止业务调用方把 session ID 直接拼到 `.boxteam/sessions/` 后定位会话；会话路径必须由权威索引解析，并校验对应物理目录。
- 迁移必须先检查目标、不得覆盖新数据；无法可靠归属到会话的旧数据应移入 `.boxteam/orphaned/` 并保留可诊断信息。
- 失败时抛出包含源路径、目标路径和操作阶段的明确异常，不得返回虚假默认路径。
