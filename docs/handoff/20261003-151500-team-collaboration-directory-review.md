# 团队协作目录审查与处理记录

审查日期：2026-10-03（北京时间）。文档与目录整理阶段没有派生 subagent，没有继续 OpenSpec；用户随后授权一次只读模型探针，已完成，未启动业务实施团队。历史来源：[恢复部署地址为127.0.0.1 (2)](codex://threads/01a0e109-e728-7282-b752-ced8a35fd5ef?hostId=remote-ssh-discovered%3Atest2_hyf_codex2)，已读取收口轮及前一轮记录。

## 已观察到的事实

- 仓库 HEAD 为 `9bc4246b`。本轮编辑前用独立索引 `read-tree HEAD` 检查，工作树零残留；共享索引仍会报告大量 `D/MM`，属于陈旧索引。没有修复或清空共享暂存区。
- Git 当前登记两个 worktree：本仓库，以及 `/data/hyf/20260629_agent/mcp-test-worktrees/collaborative-debugging`。后者属于其他任务，本轮不移动。`/tmp` 中的整仓目录不能直接称为 worktree。
- `/tmp` 顶层有 17,977 项，其中 9,609 个目录、397 个 `.idx`、2,312 个 `.log`、1,548 个 `.txt`。这只是本机总体统计，不能归因给一个聊天，更不是可整批删除的清单。
- `out/tests/temp/` 本轮开始时已有 654 个目录；新增本轮 `team-collaboration-docs` 后为 655。缺少日期、run_id 和统一归属表，重复轮次名称让回收和追溯困难。
- 历史收口轮明确创建 `/tmp/handoff_base2/repo`，整体 archive 解包后复制 `.env`，链接根仓库的 `.venv` 与 `node_modules`，运行基线对照；该目录清理前约 270MB；本轮核验可重建后已删除。`/tmp/arch_before` 约 259MB，但尚未据本次读取的记录完整核实其归属，不能顺带清理。
- 收口轮还留下 `/tmp/handoff_check*.idx`、`h3.idx`、`h4.idx`、`hf.idx`、`handoff_land.idx`、`handoff_doc.idx` 及多份测试日志、提交正文和 HEAD 记录；部分名称过于通用，搬移前仍须检查是否被其他任务复用。
- 交接引用的 `out/tests/temp/{arch_owner_dedup,review_p1_concurrency,review_round5,...}/artifacts/` 已在正确产物根下，本轮保留原位置与引用。正式测试输出不随此次整理迁移。

## 为什么会出错

| 问题 | 已有证据与原因 | 对应调整 |
|---|---|---|
| 大量目录无法回收 | 随手命名 `/tmp/h3.idx`、多个 `owner_probe*`，缺少任务归属、完成登记和回收清单 | 主代理预先分配绝对路径，按日期/run_id/任务分组，结束时登记 |
| 提交吞入他人内容 | `cbc15fc3` 漏掉独立索引前缀，误提交 701 个路径；其他事故使用旧 read-tree 快照 | 主代理串行集成，独立索引加提交前后复核；索引并不是工作树隔离 |
| 判断“文件被删”失真 | 共享索引陈旧；历史 242 条告警中 241 条磁盘内容与 HEAD 相同 | 新独立索引检查，结合文件存在、blob 与未跟踪文件核对 |
| 模型与上下文失控 | 省略模型会继承默认模型；省略 DeepSeek effort 曾导致 400；完整历史 fork 有参数限制 | 技能明确模型/effort/fork，派单只传必要上下文并核对复用记录 |
| 模型可用性误判 | 整理时擅自给 gpt-6-luna 添加 provider 前缀，并仅凭覆盖选项列表推断不可用；用户纠正后无前缀实测成功 | 使用用户指定的精确 ID，核对实际创建、回复与 turn_context，不把候选列表当完整注册表 |
| 测试与生产错配 | 历史单独覆盖测试后触发 410 重连紧循环、进程内保护失效 | 整体 archive 对照，进程外超时和内存保护，重测试限制并发 |
| 源码隔离了但运行数据仍互撞 | 历史观察到共享 gateway.sqlite、401 token 和后端 setup 退出；快照复制 .env | 同时分配运行数据、端口与测试工作区；保留原始错误，勿因基线失败就宣称根因已明 |
| 旧文档持续给出错误指令 | 旧交接仍含 commit pathspec、共享索引 reset、固定模型和阶段性端口禁令 | 旧记录加失效提示，技能成为当前调度方法的唯一维护位置 |

模型不是目录混乱或所有语义回归的根因。共同文件写入、共享暂存区、无路径归属、运行数据复用和未经独立核验的结论，换模型后仍可能复现。

## 已确认的目录方案

用户已确认两个允许根，日期按北京时间，run_id 使用 `HHMMSS-任务短名`：

```text
out/worktrees/YYYY/MM/DD/<run_id>/<task>/
out/tests/temp/YYYY/MM/DD/<run_id>/<task>/
  artifacts/   报告、截图、日志、搬移清单
  workspace/   临时测试业务数据
  snapshots/   完整 archive 副本
  git/         独立索引、提交正文、核验状态
  runtime/     独占运行控制数据
```

用户已确认采用仓库内两个根。既有正式测试路径保持不变，既有报告不为整齐而重命名。另一可选方案是将 worktree 放在仓库同级的专用根，避免仓库内递归扫描；代价是代码与产物分散到两个物理根。

只靠技能中的路径白名单是行为约束，不能强制工具禁止越界。先按明确派单、cwd 验证和结果复核执行；若仍有越界，可另立开发任务增加创建/回收工具或文件系统隔离，本轮不擅自引入脚本框架。

## 旧目录处理方案

用户已选择核验后删除可复现副本、保留报告。剩余索引、提交记录、探针和日志分类整理到：

`out/tests/temp/2026/10/03/legacy-thread-01a0e109/`

其中 `git/` 保存索引、提交和 HEAD 文件；`artifacts/legacy-files/` 与 `legacy-agent-data/` 保存原日志和探针。`artifacts/snapshot-deltas/` 每份副本记录 Git 基线、删除清单、权限变化与保留的 overlay 文件；重建时先 archive 基线，再覆盖 overlay 并应用删除清单。所有保留文件以 SHA-256 核对。整仓可重现部分删除，不创建指回 `/tmp` 的兼容软链接。通用文件名有复用嫌疑、正在使用、Git 登记或仍被当前脚本引用的项目先登记，不搬。

注册 worktree 使用 `git worktree move/remove` 管理；普通快照在核验归属后搬移。未提交代码或未知对象不能用 force 删除。遗留副本中的凭据文件不打印正文、不提交；与当前显式来源逐字相同的重复 `.env` 随副本删除，不同的内容作为保留项维持原权限。不得误删 `.venv` 或 `node_modules` 软链接所指向的主仓库依赖。

用户已决定：

1. 采用仓库内的日期/run_id/任务目录方案。
2. 核验后删除可复现副本，保留报告与核验映射；归属不明或仍在使用的内容登记而不删除。
3. GPT 技能使用 **`gpt-6-luna`（无 provider 前缀）/ max**；15:43 只读探针成功，子会话记录也确认该模型和 effort。实际创建失败时明确报告错误，不替换模型。

共享索引的重建属于另一个有暂存内容风险的操作。本轮保留它；现有独立索引流程允许先完成文档整理，不需以重建为前置条件。

## 本轮产物与实际处理

- 两个技能入口及共同流程位于 `.codex/skills/`；移除根 `AGENTS.md` 中固定模型和重复调度细则。
- 新旧交接明确历史状态与当前技能入口，旧危险命令加失效提示。
- 本轮核验产物最终保存于 `out/tests/temp/2026/10/03/legacy-thread-01a0e109/artifacts/`：`cleanup-summary.md`、`cleanup-manifest.json`、`final-verification.json`、`legacy-inventory.json`、`snapshot-verification.json`。原任务检查索引、一次性工具与输入清单已在收尾时删除。
- 遗留目录处理完成：删除 14 份可重建整仓副本，分类搬移 67 项；独立重读校验 23092 个保存文件全部通过。详情见 [清理结果](../../out/tests/temp/2026/10/03/legacy-thread-01a0e109/artifacts/cleanup-summary.md) 与 [完整映射](../../out/tests/temp/2026/10/03/legacy-thread-01a0e109/artifacts/cleanup-manifest.json)。
- 本轮验证只针对文档、技能格式、链接与变更范围；没有声称复跑历史业务测试或验证 OpenSpec 实现。

## 工作区迁移与最新目录约束

用户随后授权完整迁移、停写切换与旧副本清理，又明确要求停止复制对后续开发无用的目录，并且后续不再使用这批临时工作区。迁移前错误选择全量复制，持续约 8 小时 16 分、传输约 165GB，把历史测试运行目录和参考仓库编译输出一起带入；数百万小文件导致扫描、复制及校验耗时，根因是没有先确定用途和保留范围，将可重建产物当成开发必需文件。

迁移已改为保留源码、Git、配置、主仓库依赖、参考源码、真实开发运行数据、审查报告和独有差异；排除旧测试工作区、重复运行目录、整仓快照、打包与编译输出。保留的开发范围约 6.65GB，报告约 1.46GB，真实运行及控制面数据约 66.5MB；这些是选择性同步统计，不是最初错误全量复制的大小。3438 个 Git 跟踪文件逐一校验一致，HEAD、原共享索引与未提交文档均保留。

2026-10-04 00:58（北京时间）已将物理根切换为 `/data1/hyf/20260822_agent/vscode-graph-agent-opencode`，原路径成为软链接；另一个登记 worktree 仍可访问同一个 Git 公共目录。本次迁移证据位于 `out/tests/temp/2026/10/03/160552-workspace-migration/coordinator/artifacts/`。此目录只保存迁移证据，没有测试业务工作区。

2026-10-04 02:15:56（北京时间）清理完成：系统盘旧副本 60 个分组、数据盘误复制副本 694 个分组均无失败，两处旧目录已不存在，最终清理退出码为 0。系统盘可用空间从 84,155,809,792 字节增至 322,082,041,856 字节，增加 237,926,232,064 字节（约 238GB，按清理前后文件系统可用空间差值计算）。`cutover-state.json` 状态为 `migration_complete`；详见 `cleanup-source-final.json`、`cleanup-destination-final.json`。原路径只保留软链接，当前仓库物理目录位于数据盘，设备号为 2065。

后续不复用本次排除的历史临时工作区；派单使用数据盘物理 cwd。已经确认的日期/run_id/任务布局仍用于新 Git worktree 与必要产物，不能把它理解为允许复制所有旧测试环境。需要恢复历史业务工作区时，应先列出具体路径和用途询问用户。

恢复服务时发现软链接无法保留路径派生身份：`Path.resolve()` 取得新物理根后，默认工作区的 Gateway ID 改变，终端状态拒绝启动。已通过现有 `GatewayStateStore.replace_workspace_registry` 接口及相应 JSON 更新同步维护身份、导航和索引，未修改生产源码。

随后默认工作区的旧 UUIDv4 会话 catalog 被当前 UUIDv7 校验拒绝；该 SQLite 文件与迁移前逐字节一致，属于旧数据与当前源码不匹配。用户明确回复“旧数据直接扔”，因此删除默认工作区 `.boxteam/` 和相应会话索引，由正式服务重建；普通工作区文件与全局配置保留。没有继续 OpenSpec 或实现旧会话 ID 转换。

另一个登记工作区 `/data1/hyf/test_workspace/drive_bicicle` 的旧 JSON 会话索引缺少当前所需的 SQLite catalog，最初因超出目录搬迁范围保留。用户随后明确授权清空其 `.boxteam/` 旧数据并恢复连接；已停服务、删除该目录及对应 Gateway catalog 缓存，核对普通文件未变，再通过完整 `bun run dev` 重启。2026-10-04 02:06（北京时间）核验两个工作区均为 `ready`、`connection_error: null`，激活工作区仍是默认工作区。前端 8027 的 `/api/gateway/health`、`/api/gateway/workspaces`、`/api/v1/workspace` 均返回 200，响应头/体 request_id 一致；证据见 `service-verification.json`、`workspace-status-final.json` 与 `legacy-drive-data-discarded.json`。

验证额外发现：`DELETE /api/gateway/users/current` 被先注册的 `DELETE /api/gateway/users/{user_id}` 遮蔽而返回 404。只读验证使用的游客 cookie 已丢弃，服务端游客租约等待到期；未改业务源码。后续恢复缺陷排查时独立复核该候选，不把三个初始化 API 通过解释为用户访问生命周期全部正确。

切换前原共享索引 SHA-256 校验相同；切换后索引文件于 16:58:29 UTC 刷新，哈希变为 `7801bf0557f4e085b61b452b2671ab3d7047c2efd5c9873e69e0ffd84a29071f`，仍含历史 `D/M` 暂存记录。本轮没有对共享索引执行 `add/reset/read-tree`，也没有提交或改写历史；不据缓存索引判断源码丢失，继续用任务独立索引核对真实工作树。
