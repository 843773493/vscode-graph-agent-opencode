# 目录用途

工作区附件目录的唯一存储链路。`catalog.py` 只读写 schema 4；`ingest.py` 编排独立上传目录的事务发布；`store.py` 负责原件、预览、文件与 Shell 准入；`recovery.py` 按原消息接受事实结算 pin、对账运行时代次及释放关联。`fork_store.py`、`fork_claims.py` 和 `fork_import.py` 共同完成目录冻结、精确 alias、保活与目标发布。`migration/` 只提供显式 schema 3 离线迁移。

# 可修改内容

- 维护原附件 owner、受检 locator、目录关联、30 天期限、冻结来源解析及对应测试。
- SQL 结构、索引和约束维护在 `sql/`；Python 只保留参数绑定、事务与执行协调。
- `schema.py` 和旧 blob SQL 仅用于离线只读核对；旧 record 与快照只用于迁移和封存历史解码。

# 不可修改内容

- 不恢复按 digest 合并上传、旧 blob writer、即时 GC、双读或兼容转发；相同内容的独立上传拥有独立目录。
- 不扫描日期目录定位、吸收或清理未知资源；locator 必须来自 catalog 或冻结 intent，日期不按当前时间重算。
- 不改写 canonical、旧 URI、旧快照载荷与 hash；来源 locator 不得用于定位跨端目标。
- 不把目录关联当成授权，不以相同 digest 猜测 owner，也不将超时或进程身份失效当成外部操作已停止。
- 不跨正文 I/O、网络或模型执行持有 Session gate，不同时持有两个 Session gate 或数据库写事务。

# 规范

- 新目录固定包含 `original/`、`preview/`、`derived/`。原件不可变，派生区复用原文件 owner；新文件写入字节前使用公共跨平台私有权限原语。冻结 capture 不设置 live 原件的只读权限。
- URI grammar、child 编解码和目标校验唯一定义在公共资源 URI owner；原生路径在目标机器解释，不回退到另一个目标。
- 上传顺序为 topology shared → Session gate 内冻结 pin → 原 holder 内写 staging/durable → 短 gate 内复核 pin/fence/thread → 同 catalog 事务发布目录、版本、关联与 ingest。失败只清理本 intent；writer 未退出不得释放 holder。
- 运行时关联包含 tracker 实例与 generation。冷上传、fork 和 queued 输入由原持久事实保活；旧代解绑不得移除新代关联。最后一次零关联才起算 30 天，重关联取消期限，重启不延长已提交期限。
- GC 复用原文件 mutation lock、目录 holder 和唯一外部 lease 账本；删除前事务复核关联、ingest 与 fork claim，先标 deleting 再删物理目录，失败保留 locator 并幂等重试。
- fork 同工作区保留目录关联，跨工作区只复制冻结 manifest。网络统一使用原 files/chunk；新目录快照显式版本，旧 v2 blob 载荷只解码。alias 按精确消费 Session/Thread 与已提交 claim 解析，未映射 URI 不猜测归属。
- `maintenance.py` 的共享/独占跨进程锁保护线上与离线切换；关闭失败保留未关闭连接和锁。schema 3 须显式离线迁移，未知版本或损坏结构直接报错，不能初始化空库掩盖丢失。
- Agent 团队、成员、委派及会话协作通信按用户裁定保留 TODO，暂缓实现与专项验证。
- 修改后完成相关 Ruff、compileall 与带进程外超时和内存保护的定向测试；真实入口与冻结输入验收不能由手填历史代替。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
