# 目录用途

附件目录迁移的离线维护入口。`preflight.py` 只读核对 schema 3 的持久状态、原件及权威 locator；`backup.py` 在独占维护锁内生成并核验可恢复备份；`directories.py` 消费备份，在其 `converted/` 内准备独立目录；`catalog.py` 准备和核验对应的隔离目标 catalog，保全旧 owner 与终态 fork 原文；`__main__.py` 提供显式命令。

# 可修改内容

- 维护迁移准入、SQLite backup API 协调、原件与冻结历史保全、备份清单及持久切换记录。
- 迁移实现落地后在本目录接续 schema 3 → 4 的转换与中断恢复。

# 不可修改内容

- 不作为第二套线上附件 store，不从日期目录吸收资源，不修改已封存历史或猜测过去的授权。
- 不把取得维护锁或备份成功当作版本切换完成，不关闭真实外部进程或伪造其终态。
- 不覆盖备份的原 `data/`、manifest、journal、源数据库或原件；未知版本、未完成操作及损坏数据明确拒绝并保留。

# 规范

- 使用原附件 owner 的维护锁。SQL 结构仍由现有 SQL 文件负责，此处只保留读取和执行协调。
- 外部占用同时核对通用 lease 账本和 Terminal/Browser 领域记录；复用原 owner 的格式与终态闭集，不调用恢复或修改关联。已核实终态的领域记录必须逐字进入备份，创建中、待核实及 dispatching fork attach 明确拒绝。
- prepare 仅准备 schema 3 备份；convert/verify-directories 只处理备份内独立目录。prepare-catalog/verify-catalog 在 `converted/catalog.sqlite` 准备 schema 4 的目录、文件版本、关联及不可变来源绑定，DDL 唯一定义在 `../sql/managed_directory_schema.sql`。旧线上 owner 仍只支持 schema 3；线上上传/读取、新 fork/ingest 及切换/撤销仍 TODO，不能把 catalog_prepared 当成线上可用。
- 每个旧 attachment_id 单独复制原件到 `original/content`，已有预览逐字复制到 `preview/512.webp`，`derived/` 留空；不使用硬链接，不重新生成预览。完整旧 owner/blob 行保留在转换清单，UTC 日期来自 owner 创建时间。released 的释放时间与显式 retention 保护保留，tombstoned 不造目录。
- 转换和核验复用原维护锁互斥；复制中断只回收本计划的 `.partial` 副本，已完成文件核 hash/length 后复用，损坏成品不覆盖。全量来源与目标校验、durability 完成后才发布目录准备完成。
- 命令只输出稳定身份、状态与计数，清单只保存相对 locator，不输出普通业务原生路径。
- 目标 catalog journal 绑定原备份、目录清单和 DDL 指纹；写库或 rename 后中断可沿同一准备身份续跑，成品必须完整核验且绝不覆盖。终态 fork snapshot_json 和 preimage 原样保存，来源表禁止 UPDATE/DELETE；新结构不得重算旧历史 hash。
- 完成物化或中止的临时 capture 可已由原 owner 清理；残留 capture 仍按原 snapshot/marker hash 核验并逐字保存。离线准入不得要求重建已清理的终态临时目录。Linux 的既有执行明细文件名可含冒号，locator 按目标原生语义保留，同时拒绝各段盘符、逃逸与符号链接；Windows 继续拒绝冒号文件名。
