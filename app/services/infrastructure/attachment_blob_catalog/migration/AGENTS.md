# 目录用途

附件目录迁移的离线维护入口。`preflight.py` 只读核对 schema 3 的持久状态、原件及权威 locator；`backup.py` 在独占维护锁内生成并核验可恢复备份；`directories.py` 消费备份，在其 `converted/` 内准备独立目录；`__main__.py` 提供显式命令。

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
- prepare 仅准备备份；convert/verify-directories 只处理备份内隔离产物，schema 保持 3。schema 4 catalog、冻结 alias/claim 解析、线上切换和撤销仍 TODO，不能把 directories_prepared 当成线上可用。
- 每个旧 attachment_id 单独复制原件到 `original/content`，已有预览逐字复制到 `preview/512.webp`，`derived/` 留空；不使用硬链接，不重新生成预览。完整旧 owner/blob 行保留在转换清单，UTC 日期来自 owner 创建时间。released 的释放时间与显式 retention 保护保留，tombstoned 不造目录。
- 转换和核验复用原维护锁互斥；复制中断只回收本计划的 `.partial` 副本，已完成文件核 hash/length 后复用，损坏成品不覆盖。全量来源与目标校验、durability 完成后才发布目录准备完成。
- 命令只输出稳定身份、状态与计数，清单只保存相对 locator，不输出普通业务原生路径。
