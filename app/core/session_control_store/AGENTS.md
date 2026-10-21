# 目录用途

`app/core/session_control_store/` 是 per-session `session-control.sqlite` 基础设施
`SessionControlStore` 的唯一实现点（原先的 `session_control_store.py` 单文件已拆分入
本包）。facade 落在本包 `__init__.py`，只保留 `SessionControlStore` 的类声明（装配各垂直方法族）、连接生命周期方法（`__init__`/`connection`/`close`/
`_ensure_open`/`_begin_immediate`/`_write_transaction`/`_connect`）与模块级公开符号的
再导出；既有方法族由本Store装配。新增逻辑按明确输入/事务连接组织为函数或组件，不要求继续增加mixin，也不能在MRO中隐藏新依赖。

本包承载的族：thread creation record（`thread_creation_record.py`）、创建发布与终结
（`thread_creation_publish.py`）、初始 execution intent（`execution_intent.py`）、
collaboration成员账本（`collaboration.py`）及同ledger的board/event/fanout/逐recipient记录（`collaboration_fanout.py`）、当前schema初始化
（`_schema.py`）、共享 SQL 常量 `sql.py`，以及稳定的 `ThreadGraphBindingRecord`
值对象（`contracts.py`）。`background_work.py` 承载精确Thread的版本化后台任务、领取token和checkpoint，`background_mailbox.py`承载同一Thread的持久消息、去重和消费回执；两族仍由本Store同一事务入口读写。thread catalog/fence、operation lease、
owner binding 与跨 Session 通信 ledger 各自已在同名兄弟子包中承载，不在本包重复实现。

# 可修改内容

- 可以维护 facade `__init__.py` 中的 `SessionControlStore` 类声明、连接生命周期方法、
  `SCHEMA_VERSION` 与 `__all__`/再导出面。
- 可以维护各方法族中的方法体、显式函数/组件和族专属行投影dataclass；新增协作不得建立第二ledger。
- 可以维护 `sql.py` 中收敛同语义重复的 SQL 常量；新增共享常量时落在该模块，不在各族复制。
- 可以维护 `contracts.py` 中跨 catalog writer/reader 共用的纯行值对象；不得在这里新增 GraphBinding 的第二份持久化 authority。
- 可以维护随本包落地的四段式 `AGENTS.md`。
- 可以维护后台任务与邮箱方法族的typed记录、DDL和读写，沿原SessionControlStore唯一owner；运行时registry只保存句柄/状态镜像，不能新增第二持久恢复权威。
- 可以维护collaboration mutation与fanout同事务的typed记录及逐recipient状态；pending source和canonical正文继续归各Thread原owner。
- 可以维护对应的单元测试；测试仍放在 `tests/unit/core/` 下。

# 不可修改内容

- 公开入口`SessionControlStore`及模块导入路径`app.core.session_control_store.SessionControlStore`保持单一。
  已授权业务变更可扩展方法/字段/构造合同，必须迁齐调用方与测试，不保留旧签名兼容。`__init__.py`对
  `ThreadCreationRecord`/`ThreadExecutionIntent`/`CollaborationMember`/`_INITIAL_STATE_VALUES`
  等仍在使用的模块级符号继续显式导出；删除前全仓核caller。
- 不得为兼容旧调用点保留转发方法、旧模块shim或双套实现；跨族复用同一权威连接/事务，不借隐含self私有方法制造第二writer。
- 不得静默改动搬迁方法的异常类型、错误消息或注释；搬迁必须逐字保留语义（禁止把
  `RuntimeError` 改成 `TypeError` 之类）。
- 不得在本目录重新实现 thread catalog/fence、operation lease、owner binding 或通信 ledger
  的 DDL/读写；这些分别归 `session_control_thread_catalog/`、`session_control_operation_lease/`、
  `session_control_thread_owner_binding/`、`session_control_communication_ledger/`。
- 不得静默吞掉不一致：库被外部改动、record/intent 状态冲突、CAS 失败必须直接抛错。
- 不得恢复`background_tasks.json`或无持久owner注入的内存emit旁路；后台任务/邮箱必须精确Thread归属，agent_id仅selector，不推导main thread。

# 规范

- 静态结构 SQL 位于同包 `sql/`，所属 Python 模块只加载和协调原执行顺序；SQL 资产不自行提交事务，不保留内联结构副本。

- 既有方法族mixin的跨类静态方法引用用具体
  mixin 类名限定（例如 `ThreadCreationRecordMixin._validate_frozen_json_text`），每个方法在
  `SessionControlStore.__mro__` 中只允许定义一处（`__init__` 除外）。
- 各 mixin 只依赖宿主类提供的 `database_path`、`_connection`、`_closed`、`_ensure_open()`、
  `_begin_immediate()` 与 `_write_transaction()`；不得假设其它族的私有方法存在，跨族调用须
  经由定义该方法的 mixin 提供的公开/受保护接口。
- 表 DDL 与列清单常量的唯一定义点在其所属族模块；`_schema.py` 只编排建表顺序，不复制 DDL。GraphBinding 的唯一运行时 authority 是 thread_catalog 四字段，旧 JSON 不读、不迁移。
- 任务版本化恢复描述、首次checkpoint和既有operation owner的任务lease在本Store同一事务提交后才执行，不留孤立lease或另一套补偿writer；time emitter输出消息与任务checkpoint也在同一写事务提交。收集工具返回batch不等于消费提交，ack依据真实canonical提交及稳定identity恢复对账，不从正文或内存返回值推断。外部执行不得持有写事务或lifecycle gate；未知外部效果保`reconcile_required`，不能盲重放。任务活跃lease和迟到callback归既有residency/generation owner，本包不复制graph或idle策略。
- 协作mutation、event和fanout在同事务冻结generation/revision/hash/recipient集合；逐recipient投递及Registry发布归编排owner，本Store不加载cold runtime。禁止JSON board或分步event旁路；当前schema严格拒绝旧开发形态，不迁移或懒补DDL。
- 错误分类沿用 `session_control_store`：`KeyError` 目标行缺失、`RuntimeError` 库被外部改动
  或语义冲突、`ValueError` 输入形态非法、`TypeError` 输入类型错误。
- 编写源码执行目标静态分析；按完整受影响调用闭包完成真实事务/schema/恢复验证，测试使用进程外保护。批次内复用未变检查，不因每个文件修改重复整组测试。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
