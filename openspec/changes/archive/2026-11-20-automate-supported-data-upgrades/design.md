## Context

动机见 proposal。现有正常 Launcher 会调用 configs.boxteam 初始化；配置布局、日志／连接 ID 与工作区身份各有原迁移 owner，SQLite 有版本有序链。当前没有已完整验收并发布的 release，支持来源只沿已有迁移合同，不推测额外历史格式。

## Goals / Non-Goals

目标是将确定的受支持升级接入原启动／重启生命周期，保留原 owner、备份和历史身份。未知版本及数据取舍仍由用户决定。普通 loader、业务请求与热配置变更不承担离线结构升级；不建立升级数据库、恢复框架、影子引用或运行时双读。

## Decisions

### 生命周期先停写，再由原 owner 发布

Launcher 正常启动发现同 Home 有存活实例时，仅对识别出的受支持升级执行只读预检，再沿 stopLauncher 核实停止，取得原 Launcher 锁后初始化并重新启动。未知版本预检失败保留原实例；同版本重复 start 保留原实例冲突语义。restart 继续先停止后初始化。

初始化复用原维护 owner 处理配置和身份；布局也要在数据库 owner 已停止写入后提交，原 JSONC 字节先备份。Gateway 的登记只用于明确本机迁移对象，不接管 Workspace 正常配置监听；其它 Home／远端工作区不按中继本机路径访问。

### 版本和恢复方向各自保留

Gateway 配置当前为 v5，连接身份记录仍为 v4，两者分别判断。正常当前版本不生成新备份或重写正文。中断正向迁移继续原冻结计划；正在回退的迁移完成原方向并停止本次启动。已明确回退的 journal 保留用户选择，只有显式重做原迁移才重新升级。

旧 Gateway 外观只有原默认值（`warm`、空自定义主题）时自动移除；自定义值没有无损 Workbench 映射时保留原件并要求用户决定。候选沿实际转换函数在内存中校验目标 schema，不能先停旧、拆布局或创建迁移备份后才发现已知冲突。

旧辅助服务的原默认地址随插件替换退休，自定义地址仍要求用户决定。用户另明确批准仅对精确空 `records`／`leases` 的旧 `resources.json` 保留原字节备份后退休；原资源 owner 在 Workspace 停写锁内执行，非空或其它形状不推测兼容、不参与普通请求双读。

用户追加授权固定实验来源 `bf0f2e1d9d037efcb0210c458fa157fb4d54a27a` 的真实历史恢复。来源事实由该完整源码经真实消息入口生成，合同为 catalog v11、Session control v18、rollout v26 和带 v1 提交清单的 Trace JSONL；目标分别为 catalog v12、Session control v20、rollout v28 与 Trace SQLite schema 3。支持已结算普通会话和 main Thread；fork、child Thread、未结算创建／删除及尚未登记转换的非空后台账本在首写前明确拒绝，不猜测其恢复语义。

用户明确允许丢弃无法补齐事实的实验数据，因此缺少 dispatch 的旧 job_started、缺少控制 revision 的旧 goal_cleared 不导入当前 Trace，输出按事件类型统计丢弃数量，不伪造默认值，也不新增运行时历史兼容协议。其余 Trace 转为当前事件 envelope，重新计算逻辑字节偏移，旧传输游标可能失效；旧 JSONL 及索引完整保留在迁移备份，在线只用 SQLite。现行新事件登记 `format_version=1`，与 SQLite schema 版本独立；未来不兼容事件字段变化必须递增此合同。

用户另明确授权将旧 Provider ID 转为同一 Provider 的 default 模型引用，并重算对应创建 metadata 的 preimage；迁移前先核原 hash，原创建意图和原 hash 保存在完整 catalog 备份中。会话／Thread 身份、canonical 正文、item hash 和已封存请求不重写。此前“未知来源、暂不导入”的裁定由本次授权替代；没有 release 发布基线，不把该实验验收扩大为任意旧版本支持。

### 会话历史复用原跨资源发布原语

配置初始化只读读取 catalog 的已登记 locator，不扫描目录猜测来源；全部支持边界在配置首写前预检。停写并取得原 Workspace SQLite ownership 后，各 owner 转换独立候选；合入已提交 WAL 并关闭旧句柄后，以完整文件作为原维护 journal 的资产，复用备份、CAS、启动屏障和中断续跑。普通读取不导入旧格式，不维护正文双份。显式恢复入口为 `uv run python -m configs.maintenance.session_history --workspace <绝对路径> --restore`；恢复保留原方向，不自动再升级。

### SQLite 沿唯一有序链升级

数据库 owner 在锁内读取版本账本，支持的旧库在原写事务内取得含已提交 WAL 的独立备份；有序 SQL 和 Python 回调在同一事务内执行，迁移后完整性及外键校验成功才提交。新空库沿原初始化，当前结构不做全库检查；未知版本拒绝。共享换代只接受当前结构，旧结构先走完整停止／启动，不能让仍存活的旧进程继续读写新结构。离线 Trace 升级使用独占 owner。

### 任意格式转换按域、两端版本和格式组织

新增格式转换源码放在 `<业务域 owner>/migrations/`，配置域沿用 `configs/migrations/<domain>/`；文件名统一为 `v<来源版本>_<来源格式>_to_v<目标版本>_<目标格式>.<实现语言后缀>`。业务域由目录体现，文件名直接显示两端版本和格式，不散落在原业务目录，不另建平行的 imports 或 recovery 目录。跨资源生命周期协调仍留在原维护入口。

```text
<业务域 owner>/migrations/
├── AGENTS.md
├── v1_jsonl_to_v2_sqlite.py
├── v2_sqlite_to_v3_jsonl.py
└── v3_jsonl_to_v4_jsonl.py
```

这些是命名示例，不登记实际支持来源或预造文件。格式名使用实际格式的小写标识，后缀取决于实现语言；JSONL、SQLite、JSON、CSV 等均可作为来源和目标，不固定转换方向。同格式结构变化也使用这一命名；后续发布版换回原格式是新的正向迁移，采用新的目标合同版本，不当成显式回退。

两端版本指该业务域已明确登记的数据格式／结构合同，不混同软件 SemVer 或数据库 schema 版本；旧数据无版本时先核实其来源及版本归属，不为命名虚构 v1。多段版本进入 Python 模块名时将点转为下划线。执行链继续由原 owner 清单明确登记，不扫描文件名排序、不新增版本映射账本。

已有有序 SQL 如 `001.sql` 保持名称与顺序，SQL 结构和迁移语句留在 SQL，执行协调留 Python；仅发生数据库结构变化才提升 schema 版本。实际增加格式转换时按需创建迁移目录，并同步只允许 SQL 的 owner 约束；本轮不批量搬迁已有维护模块，固定来源的实际转换按上述窄合同登记。实现和审查遵循 `.codex/skills/version-upgrade-conventions/SKILL.md`。

## Risks / Trade-offs

- 迁移失败或磁盘不足 → 原件／备份保留，原启动失败，不开放部分升级的服务。
- 外部修改或来源身份无法确认 → 复用原 CAS／身份预检，明确失败，不猜测映射。
- SQLite 全库校验有成本 → 仅在实际升级时执行，当前版本暖读不检查。
- hyf_win 内存不足 → 不降低产品内存阈值，先验收无 Browser 的升级链，完整插件发行验收独立保留未完成。

## Migration Plan

在根验证受支持配置、身份和 SQLite 事务，精确提交后同步冻结候选到 hyf_win 的隔离目标。经正常初始化自动升级，再由真实 Gateway 托管 Workspace 启动和重启，核身份及备份；未知／取舍场景核原件不变。旧产品入口生成的完整历史经当前正常 initialize、Gateway／Workspace 两次启动独立验收；备份、丢弃报告与重入不重写均保留原始证据。新增历史链本轮先在 Linux 验证，已有 Windows 结果不代表新候选通过。正式发行基线仍按首次发布处理，不自动更新 dev/release/main。
