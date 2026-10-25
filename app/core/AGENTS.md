# 目录用途

`app/core/` 存放不带具体业务语义的后端通用内核，包括环境读取、路径契约、标识符、事件总线、后台任务注册、Trace 中间件和一次性存储迁移。

## 顶层模块导航

先按调用目的定位，再进入专用子包。下面的分组描述模块职责，不表示运行时调用顺序：

```text
app/core/
├── 环境、身份与路径
│   ├── env.py / config_sources.py / distribution_identity.py
│   ├── user_identity.py / workspace_identity.py / mounted_workspaces.py
│   ├── path_utils.py / path_segments.py
│   ├── user_storage_migration.py
│   └── logging_config.py
├── 请求、模型与执行上下文
│   ├── trace_middleware.py / job_context.py
│   ├── model_request_context.py / model_delta_context.py
│   ├── execution_admission_context.py / session_thread_context.py
│   ├── turn_execution_scope.py / cancelable_stream.py
│   └── session_interrupt_state.py / history_loading.py / checkpoint_config.py
├── 标识、内容与协议基础
│   ├── identifier.py / hashing.py / bounded_json.py
│   ├── resource_uri.py
│   └── exceptions.py
├── 并发、生命周期与后台工作
│   ├── key_lock_pool.py / lifecycle.py / session_lifecycle_gate.py
│   ├── shielded_cleanup.py / phase_observer.py / job_event_bus.py
│   ├── background_task_contracts.py / background_task_registry.py
│   └── background_message_bus.py / thread_residency_contracts.py
├── 文件系统与持久化原语
│   ├── atomic_fs.py / file_permissions.py / sqlite_state.py
└── 会话创建、定位与专用持久化基础
    ├── session_control_primitives.py
    ├── session_catalog_resolver.py
    ├── session_creation.py / thread_creation.py / session_subtree_delete.py
    ├── session_catalog_store/
    ├── session_control_store/
    ├── session_control_thread_catalog/
    ├── session_control_thread_owner_binding/
    ├── session_control_operation_lease/
    └── session_control_communication_ledger/

专用存储子包内部的 SQL、schema、投影与读写 owner 见各子包对应的 `AGENTS.md`。
```

## 共享原语与专用子包说明

- `background_task_contracts.py`：精确Thread后台任务、版本化恢复描述、步骤checkpoint与邮箱消费identity的纯typed合同；不导入编排、handler实现或重型abstractions聚合出口。运行实体与执行策略分别由既有registry和服务owner承担。
- `thread_residency_contracts.py`：residency blocker及其source port的纯合同，供core registry与编排owner共同消费；不计算idle策略、创建runtime或反向导入`app/services/orchestration/`。
- `resource_uri.py`：资源 URI 的唯一纯 grammar、值对象与构造器，供 domain、Gateway 和 infrastructure 直接消费。只校验逻辑地址，不访问 catalog、正文、文件系统或网络；资源绑定、revision、能力和联邦策略仍归各自 owner。旧 `resource_platform/virtual_resources/grammar.py` 下线，不增加重导出 shim 或第二解析器。
- `atomic_fs.py`：文件级 durability 原语（`fsync_directory`/`fsync_file`/
  `atomic_write_bytes`）。会话创建、child thread 创建与子树删除共用同一套
  tempfile + fsync + os.replace + 目录 fsync 语义，单点承载。
- `file_permissions.py`：私有文件与目录权限原语。POSIX 使用 `0600/0700`，Windows 通过 `icacls` 移除继承权限并只授予所有者、SYSTEM 与本机管理员；不承载凭据格式或业务策略。
- `key_lock_pool.py`：进程内 per-key 串行锁的中立实现（`KeyLockPool` 固定分片锁池）。
  同一 key 经 `crc32` 恒命中同一把 `asyncio.Lock`，锁对象数量恒为分片数，不随历史
  key 数无界增长。会话创建、child thread 创建与子树删除三条链路共用，单点承载。
- `session_control_primitives.py`：per-session `session-control.sqlite` 各垂直链路共享的形态原语（`CONTROL_DATABASE_NAME`、`SHA256_HEX_PATTERN`、`EXECUTION_BINDING_ID_PATTERN`、`EXECUTION_JOB_ID_PATTERN` 与 `validate_claim_fields`）。只放跨子包共用的形态约束与校验器，不放任何表的 DDL、行投影或读写方法。
- `session_control_thread_catalog/`：thread catalog 与 lifecycle fence 一条垂直链路（main/child 权威指针、生命周期闸门 CAS、已发布 child 的冻结 locator 解析、当前 GraphBinding 列形状校验）。只放这两张表的职责；creation record、execution intent、operation lease、owner binding 与通信账本不放这里。
- `session_control_thread_owner_binding/`：thread owner binding 字段槽一条垂直链路（`thread_owner_bindings` 行投影、canonical JSON 列表槽解析、2.1 负面合同校验、ensure/get/update，以及该表行插入的唯一 SQL 实现）。只放 owner 侧记录槽；prefix epoch 的语义仍属 ContextStore domain owner，ToolSet applied 唯一权威为 sealed assembly binding，本表不得保存 desired/applied ToolSet 镜像。
- `session_control_operation_lease/`：通用 operation lease 一条垂直链路（`session_operation_leases` DDL 与非终态索引、行投影、create-or-get 幂等准入、fencing token CAS 链与读取）。只放持久准入/操作 lease；`SessionOperationLease` 的 typed 字段集与状态闭集仍由 `session_lifecycle_gate.py` 单点定义。
- `session_control_communication_ledger/`：跨 Session 通信 ledger 一条垂直链路（`communication_outbox`/`communication_inbox` DDL 与 target_accepted 索引、行投影、source 侧 create-or-get 与状态 CAS、target 侧 create-or-get/领取/绑定/失败记录与读取、kind=reply 双端因果证明）。只放这两张 telemetry 账表；typed 合同的 field 集与状态闭集仍由对应 facade 单点定义。
- `session_control_store/`：per-session `session-control.sqlite` 基础设施 `SessionControlStore` 的唯一实现点（facade 在 `__init__.py`，保留类声明、连接生命周期与模块级符号再导出）。其下按垂直链路分 mixin：thread creation record（`thread_creation_record.py`）、创建发布与终结（`thread_creation_publish.py`）、初始 execution intent（`execution_intent.py`）、collaboration 成员账本（`collaboration.py`）、当前 schema 初始化与版本拒绝（`_schema.py`）、共享 SQL 常量（`sql.py`）、精确Thread后台任务/typed checkpoint（`background_work.py`）与持久邮箱/消费回执（`background_mailbox.py`）。后台输出与checkpoint复用同一Store事务；runtime registry只持执行句柄/镜像，不恢复旧background_tasks.json或内存独占邮箱。旧开发生成库不升级或回填；thread catalog/fence、operation lease、owner binding 与通信 ledger 仍归各自的兄弟子包，不在此重复。
- `session_catalog_store/`：workspace 导航权威库 `session-catalog.sqlite` 基础设施 `SessionCatalogStore` 的唯一实现点（facade 在 `__init__.py`，保留类声明、连接生命周期与 schema 闸门、模块级符号再导出）。其下按垂直链路分 mixin：canonical 校验器与生命周期栅栏（`validators.py`）、不可变投影 DTO 与错误类（`contracts.py`）、nodes 表读写（`nodes.py`）、fork retention claim（`fork_retention.py`）、creation record journal（`creation_journal.py`）、fork source capture journal（`fork_source_capture.py`）、共享创建行读取/投影（`creation_record_rows.py`）、subtree delete journal 与空 folder 删除（`subtree_delete.py`）、只读查询（`queries.py`）、备份与目录一致性（`backup.py`），表 DDL 与共享列清单收敛在 `_schema.py`。`write_transaction` 的事务边界与 `_bump_generation` 提交前无条件调用顺序是 8.1-F/10.3b 依赖的不变量，不得改动。

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
- 存储操作必须先检查目标、不得覆盖新数据；无法可靠归属的数据应移入 `.boxteam/orphaned/` 并保留可诊断信息。
- 失败时抛出包含稳定 owner、逻辑资源与操作阶段的明确异常；真实路径仅供原生 owner 内部访问，不进入普通响应或业务回执，不得返回虚假默认路径。
