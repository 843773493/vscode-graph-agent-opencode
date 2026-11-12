# 目录用途

工作区附件目录的唯一存储链路。`catalog.py` 只读写 schema 5；`ingest.py` 编排独立上传目录的事务发布；`store.py` 和 `working_copies.py` 负责原件、预览、文件与 Shell 准入、初始基线、释放及按需重建；`recovery.py` 按原消息接受事实结算 pin、对账运行时代次及释放关联。`fork_store.py`、`fork_claims.py` 和 `fork_import.py` 共同完成目录冻结、精确 alias、保活与目标发布。`directory_schema.py` 原子转换已知 schema 4，`migration/` 只提供显式 schema 3 离线迁移。

# 可修改内容

- 维护原附件 owner、受检 locator、目录关联、初始基线与可靠原件版本、冻结来源解析及对应测试。
- SQL 结构、索引和约束维护在 `migrations/`；Python 只保留参数绑定、事务与执行协调。
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
- 运行时关联包含 tracker 实例与 generation。冷上传、fork 和 queued 输入由原持久事实保活；旧代解绑不得移除新代关联。最后关联解除后只记录原目录的待处理事实；无占用时检查完整初始基线，有修改保留原位和处理结果，无修改且原件可靠时释放可重建工作副本，不再计算统一期限或运行周期 GC。旧明确保留承诺继续保护。
- 启动恢复的 catalog 快照和定点恢复的 fresh 校验、canonical 读取及 pin 结算复用受保护 worker；原 holder → Session gate 覆盖排队、执行及取消结算，不让后台恢复越过锁或提前关闭存储。tracker 的当前代判断仍留在事件循环，不将同步回调整体搬入线程。
- 运行时关联快照及精确 token 解绑由同一异步对账入口等待 worker；保留 pin → 队列的读取顺序。失效代次对账的原共享 holder 覆盖 worker 结算，删除 drain 等待它退出；打开控制库前重新核对精确关联，旧快照不重开已隔离节点。tracker 尚登记的 runtime 在 closing 或关闭失败时仍属当前绑定；只有原卸载 owner 可释放它。迟到旧 token 不解除新代关联，取消不遗弃原存储访问。
- 工作副本处理复用原文件 mutation lock、精确目录 holder 和唯一外部 lease 账本；释放前事务复核关联、ingest 与 fork claim，先标 releasing 再删物理目录，原账本 stopped 后提交 released，失败保留 locator、原因及待处理事实。同 ID、URI、locator 按需重建，普通重开不制造新的解绑事件。
- `.versions/sha256/` 归原 immutable file version owner，保存重建所需原件与可再生预览；不新增正文服务、第二套可变附件或消息历史。发布前可靠保存原件，缺失、损坏及未知目录内容必须明确失败并保留处理结果。
- 同目录 fork 的 preparing/reserved claim 保护发布，committed 后由目标关联保活；最后关联解除后按同一基线检查，不等待跨端导出回执。物理独立目录的来源导出仍须等待原 export release。
- 待处理目录及终态 fork 操作查询走原受保护 worker；目录 holder 按受检稳定 ID 精确隔离，普通同步 I/O、异步下载准入、fork 和释放共用同一 OS 锁。正文退出按精确 ID 重试已有待处理，实际文件修改和 Shell 准入请求检查；有修改是已完成的保留决定，不反复挂起重查。真实文件操作须在 holder 内物化目录；观察索引与引用规范化只解析 catalog locator，不据此读取正文或触发重建。
- 文件 watch 初始化只解析 locator，用单次 stat 优先核实际节点类型，仅实际缺失的固定布局使用虚拟目录语义；不取得正文 holder、不物化或阻碍最后解绑结清，真实文件和不存在的派生子目录继续拒绝。原工作区 watcher 仅向显式附件 URI 根公布内部变更及持久事件，分类和回放不得重建工作副本或暴露其它私有状态。reveal 的解析、stat 与原生派发持同一 access；外部文件管理器持续使用期没有可核实身份时保留具体 TODO，不制造常驻占用或假报关闭。
- 工作区文件写锁只覆盖基线核对与物理发布/释放，账本等待不阻塞无关文件。账本失败保留 releasing；终态 fork 暂存的快照、原生探测与删除由同一 worker 在原 import holder 内完成，取消须等它结算，不扫描未知目录。候选一次查询取得完整行，定点访问只查询相应 ID。
- Shell 工作目录的来源识别与 holder 内复核复用既有受保护 worker，保留请求上下文及原 catalog/locator 校验；holder 覆盖线程池排队与原生访问结算，取消后不进入占用发布或 PTY 创建。worker 不抢占原生 I/O，持久 lease 写入和整个 operation/shutdown 期限仍单独验收。
- fork 同工作区保留目录关联，跨工作区只复制冻结 manifest。网络统一使用原 files/chunk；新目录快照显式版本，旧 v2 blob 载荷只解码。alias 按精确消费 Session/Thread 与已提交 claim 解析，未映射 URI 不猜测归属。
- `maintenance.py` 的共享/独占跨进程锁保护线上与离线切换；关闭失败保留未关闭连接和锁。schema 3 须显式离线迁移，未知版本或损坏结构直接报错，不能初始化空库掩盖丢失。
- Agent 团队、成员、委派及会话协作通信按用户裁定保留 TODO，暂缓实现与专项验证。
- 修改后完成相关 Ruff、compileall 与带进程外超时和内存保护的定向测试；真实入口与冻结输入验收不能由手填历史代替。
- 精确 Thread 附件目录沿原关联与原件版本单次分页查询，保留独立上传身份及已释放工作副本的来源；只投影元数据，不读取原件、扫描历史或物化目录。HTTP 原 Session 读取准入覆盖查询及取消结算，不反向取得上传 holder。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
