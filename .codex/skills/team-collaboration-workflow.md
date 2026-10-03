# 团队协作共同流程与实时台账

由两个团队技能共同引用，不是第三个版本。主代理维护流程、范围和台账；subagent 交付实现或审查证据，不并发改写本文件。具体事故详情见 [目录审查与处理记录](../../docs/handoff/20261003-151500-team-collaboration-directory-review.md)。

## 授权、goal 与恢复

- 用户已授权的工作持续推进；仅请求技能优化、文档整理或 goal 建议时，不启动实施团队。明确要求创建或启动 goal 时才调用 goal 工具；普通开发无需先创建 goal。预算仅按用户明确要求设置。
- 开始或恢复时读取当前用户要求、适用的 `AGENTS.md`、交接和相关 OpenSpec；核对实际 HEAD、独立索引状态、现存目录、依赖门及待裁定项。历史测试、模型、端口和“工作树干净”记录不能代替当前核验。
- 派单前在台账明确：本轮范围与交付物、通过条件、当前切片和依赖、已知待裁定项。相关 OpenSpec 不自动全部纳入范围；勾选数也不能证明实现完成。goal 建议须标明未启动，不能当作已接受的任务。
- 按依赖推进纵向切片：每条链路完成生产实现、相关测试和旧调用清理，再进入依赖它的切片；独立链路可并行。新需求另行登记，不持续扩大当前 goal。
- 用户已授权后续按架构熵减处理问题：统一 owner 与链路，移除旧兼容和开发中间数据。源码开发生成的新旧中间数据可直接删除，不逐项询问；该授权不等于可删除源码、未集成独有改动或来源不明业务文件。
- 本 goal 中按用户最新“架构熵减、无需再问”授权自主选择统一 owner、显式身份和删除旧兼容的实现方案；必要时同步规范并记录取舍，不再请求逐项裁定。其它任务的产品语义或契约未定时给用户具体选项、影响和建议，继续其余已授权工作；已确认的模型、路径、迁移和清理决定不重复询问。环境配置问题按项目指令处理，不擅自修改全局环境。
- 用户裁定记录对应 owner、对象和精确决定；派单及恢复时引用该记录。相似措辞不能跨领域套用；审查者先核对原问题与答复，未裁定项不能因现有实现而视为定稿。

当前交接入口：[bug 猎捕与冗余收敛](../../docs/handoff/20261003-060700-bug-hunt-and-redundancy-handoff.md)。

## 常驻队伍与派单

实施期间至少保持 3 个常驻 subagent：实现、独立行为审查、架构与冗余审查；最多 15 个，包含派生后代。等待依赖的常驻 agent 不必运行重测试；收口时完成已有工作，不为凑人数制造任务。主代理统一创建与调度，subagent 不自行派生。

主代理负责范围、依赖、架构取舍、集成和台账。行为审查核验契约与回归；架构审查按 [项目架构维护技能](project-architecture-reviewer/SKILL.md) 检查职责、目录和冗余。审查者默认只读，不同时修改自己要独立审查的实现。

使用版本入口中的显式模型、effort 和 `fork_turns: "none"`；完整历史 fork 不接受模型覆盖。创建后保留创建参数，能读取实际模型记录时核对 `turn_context`；复用前确认模型与 effort，无法确认的 agent 不复用，后续派单不能换其模型。

每条派单按以下格式给出具体值；无需另建任务说明文件：

```text
任务：编号、目标、对应需求/OpenSpec 条目、完成标准
基线：提交、分支、任务所需完整补丁；适用指令与必要上下文；状态检查索引的绝对路径
工作目录：已分配的绝对 workdir
允许写入：精确文件与可创建目录；其他任务独占范围、受保护路径
运行与产物：绝对产物根、业务工作区、BOXTEAM_HOME/端口（需要时）
验证：命令、进程外保护、预期行为；审查对象的固定提交或完整补丁
边界：跨范围变更交主代理重新分配；是否允许在任务分支提交
回报：复现/判定、改动与断言变化、验证退出码、未验证项、产物和进程
```

工具没有 cwd 参数时，每次命令显式指定 workdir，先核验物理 cwd 与 Git 顶层目录。共享主工作树的派单必须提供主代理已按当前 HEAD 初始化的任务索引，状态检查显式使用 `GIT_INDEX_FILE` 和 `GIT_OPTIONAL_LOCKS=0`；裸 `git status` 只作索引观察，不能判断实际在途改动。先串行落地基线再派单；派单后发生 HEAD 变化时由主代理说明变化范围，不能由 agent 猜测。独立审查提供原始需求和稳定产物，不预告期望结论。任务所需未提交变更先审查并集成，或完整交付补丁，不能让测试与生产代码错配。

agent 空回复、报错或中断时先核对实际改动、提交与后台进程，再决定复用或替换；释放写入范围后才能交给新实现者。跨范围修改必须先重新分配。调查已有可行动结论时先回报并写入已分配产物，不等全量报告；上下文恢复时主代理重发工作目录、状态索引、产物根、固定基线与当前边界，agent 先读既有阶段证据再继续，避免重复泛读。恢复后的默认 cwd 可能回到主树；首次写入前显式核对物理 cwd、Git 顶层和分支与派单一致，每次命令仍传 workdir、编辑使用分配的绝对路径。不一致时停止写入并报告主代理，不把错误工作树的差异当作需要重做的工作。后续审查派单也重复报告根的绝对路径；agent 写报告前将目标与最近一次派单比对，路径缺失时询问主代理，不按任务短名另建目录。

## 路径与生命周期

仓库物理根：`/data1/hyf/20260822_agent/vscode-graph-agent-opencode`。原 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 为软链接；派单使用数据盘物理根的绝对路径。

用户已确认以下根，日期按北京时间，run_id 使用 `HHMMSS-任务短名`：

| 用途 | 路径 |
|---|---|
| Git worktree | `out/worktrees/YYYY/MM/DD/<run_id>/<task>/` |
| 临时产物与协作控制文件 | `out/tests/temp/YYYY/MM/DD/<run_id>/<task>/` |

任务目录按需创建：`artifacts/` 保存可复查产物，`git/` 保存独立索引与提交正文，`runtime/` 保存独占运行控制，`workspace/` 保存临时测试业务数据，`snapshots/` 保存必要的完整 archive。不预建空目录，不自行选择 `/tmp`。

- 业务工作区必须由主代理明确分配。临时真实操作使用用户已允许的默认工作区；不复用本次迁移排除的历史测试工作区、运行副本或快照。正式测试沿用 `out/tests/<同名测试路径>/` 和完整 fixture，不改其产物布局。
- 源码仓库及代码 worktree 不得注册为测试业务工作区，也不得在其根生成 `.boxteam/`。操作前核对目标；收口核查新增目录，发现越界先停止相关写入、核实归属，不删除来源不明数据。路径约束是操作规则，不等于工具已提供文件系统沙箱。
- worktree 用 Git 创建、移动和移除；递归检索与测试排除其生成根。复制前按用途确定范围，排除历史运行、缓存、打包和参考仓库编译输出；报告与独有差异单独保留。必要重现或变异整体 archive 指定提交并应用完整补丁，不只覆盖测试文件，不复制 `.env` 充当隔离。
- 独立源码不自动隔离运行状态；需要启动服务时配套业务工作区、BOXTEAM_HOME、端口与 Gateway registry。重启全组开发服务由主代理协调；重测试按资源串行或有限并行，15 个 agent 不代表 15 个重测试槽位。
- 结束时登记目录、进程、集成状态与保留理由；纯临时产物主动清理，需复查的证据保留，正式测试输出默认保留。worktree 已集成且无未提交改动后才能移除，不用 force 清理未知工作。

## 集成与索引

默认为写入任务分配独立 worktree，主代理串行集成。任务 agent 仅在明确分配的独立分支提交；共用已有工作树时只改独占文件，由主代理集中提交。保留现有共享暂存内容，状态检查使用任务自己的新索引，并检查未跟踪文件与真实删除；不能仅凭共享索引的 D/MM 判定代码丢失。

以下流程在唯一提交者控制的窗口执行。`TASK_GIT_DIR` 是已分配的绝对 `git/` 路径，`TASK_NAME` 是任务编号；执行时替换示意值，每步失败立即停止。

```bash
TASK_INDEX="$TASK_GIT_DIR/integration.idx"
git rev-parse HEAD
GIT_INDEX_FILE="$TASK_INDEX" git read-tree HEAD
GIT_INDEX_FILE="$TASK_INDEX" git add <精确路径...>
GIT_INDEX_FILE="$TASK_INDEX" git diff --cached --name-only
bun scripts/assert_isolated_index_commit.mjs record --index "$TASK_INDEX" --task "$TASK_NAME"
GIT_INDEX_FILE="$TASK_INDEX" git commit -F "$TASK_GIT_DIR/commit-message.txt"
bun scripts/assert_isolated_index_commit.mjs verify --index "$TASK_INDEX" --task "$TASK_NAME" --commit <本笔提交hash>
git show --name-status <本笔提交hash>
git merge-base --is-ancestor <提交前HEAD> HEAD
git merge-base --is-ancestor <本笔提交hash> HEAD
```

暂存只含本次精确路径，提交不带 pathspec；禁用 `git add -A/.`、`--amend`、共享索引写入和共享历史 reset/rebase。提交窗口内 HEAD 变化则核对并重建任务索引。陈旧独立索引也会覆盖并发提交，防线脚本只做事后核验，不能替代串行集成；发现吞并时取证并用新提交前向恢复。

## 独立核验与收口

- 缺陷必须有复现与契约依据；区分真实缺陷、设计有意、待接线目标。下线符号前全仓核对直接、间接调用和测试，包含 `configs/`、`tools/`、`scripts/`；不能只凭零生产调用删除 OpenSpec 接线目标。
- “纯搬迁”比较新增/删除代码行的规范化多重集，逐条核验未保留行，特别是异常类型、默认值、调用顺序、导入和注释。净减代码不能靠删断言或必要功能取得，测试通过不能代替语义审查。
- 每次代码改动执行项目要求的静态分析，Web 改动执行构建。测试走矩阵 runner，或使用 `timeout <秒> bash -c 'ulimit -d 4194304; exec "$@"' bash <命令>`；记录实际退出码，后台运行与管道不能掩盖失败。相关检查通过后，只在新改动、失败或未解决疑点出现时扩测或重跑。
- 当前切片通过对应测试、独立审查并集成后，才更新 OpenSpec 完成状态。交付前核验依赖闭合、相关契约快照、OpenSpec 校验和产品路径；涉及交互时做真实浏览器验证。基线同样失败只排除改动独有性，不能直接认定环境根因，也不能当作通过。
- goal 仅在授权范围内的必需实现、清理、审查、验证和集成全部完成后结束；未解决的必要项保持未完成。时间或预算不足时准确交代完成部分与剩余项，不虚报完成。没有必要工作时不继续制造重构或诊断轮次。

## 实时台账

更新时间：2026-10-04 04:05（北京时间）。goal 已启动；使用 GPT 全队，八个常驻 agent 分别实施、准备后续切片和独立审查。先核对本节事实再恢复，历史服务状态只表示上次验证结果。

| 项目 | 已核验状态与下一步 |
|---|---|
| 当前范围 | 用户已启动交接任务与关联 OpenSpec 实施、缺陷修复、冗余清理及技能实测改进 goal；正在核对七个关联 change 的依赖和边界，首先推进 UUIDv7 剩余验收，不自动扩展到新需求 |
| 版本与模型 | 两个版本入口维护唯一模型配置；GPT 精确 ID 的创建、回复及实际模型记录已通过 [只读探针](../../out/tests/temp/2026/10/03/154301-gpt-model-probe/coordinator/artifacts/model-probe.json) |
| Git 基线 | 文档基线 `eb9380aa`；T01 `0a73ae37`、G01/G02 `72e755eb`、S01 `ad093e0c` 已串行集成，防线与祖先链通过。主树当前仅共同技能在途修改，主代理独占；共享暂存保留 |
| 迁移与历史清理 | 数据盘物理根及旧入口软链接已核验，旧副本均删除，系统盘可用空间增加约 238GB；另删除 14 份可重建副本、分类整理 67 项、校验 23092 个保留文件。详见目录审查及其清单 |
| 业务数据清理 | 默认工作区与 Drive 的旧 .boxteam 分别获用户授权清空，普通文件保留；该授权不扩大到其他工作区。仓库根的旧终端测试目录及空 .boxteam 已另获授权删除，创建者未确认 |
| 开发服务 | 上次核验默认与 Drive 均为 ready，前端 8027 三条初始化 API 为 200 且 request_id 一致；管理 dev:status/dev:stop/dev 使用下述原环境，恢复时重新检查 |
| 验证与证据 | T01 activity 1、history 20 项通过；G01/G02主树受保护10 passed（3.54s），旧顺序变异有效。T02 generation 最终4 passed（41.69s），R04发现消息展示断言移除，当前拒绝集成、复核canonical展示合同；更宽source history projection异常另T03调查。U01 target-only恢复barrier及U02真实v4验收补齐后117项通过，A03正独立复审，未集成；完整 users.py Ruff 有18条既有诊断 |
| 明确裁定 | `assembly_ref=ResourceIdentity`；统一寻址拒绝码 `unsupported_view`；Gateway ID `current` 创建拒绝；config layer只表示逻辑来源、读侧sqlite换runtime_override、优先级不变、快照独立。最新架构熵减授权下采用delegate发布前可恢复准备，不回滚published child。证据 `coordinator/artifacts/owner-decisions.json` |

活动任务统一根：`out/tests/temp/2026/10/04/024121-team-execution/`；八个 agent 的创建参数与实际 turn_context 均已核验为 `gpt-6-luna / max`，创建时均 `fork_turns:none`；新增模型证据见 `coordinator/artifacts/model-context-latest.json`。下表路径均相对物理根，派单始终给绝对路径。主代理独占技能与 OpenSpec 台账；S01 仅在独立 worktree 修订两项已裁定规范，不勾实施任务。

| 编号 / agent | 状态与范围 | 工作目录 / 产物 | 验证与下一步 |
|---|---|---|---|
| U01/U02 / uuidv7_implementer | U01统一隔离proof，已去completed旧特例；U02新增真实ses_UUIDv4存量验收测试 | out/worktrees/2026/10/04/024121-team-execution/uuidv7_implementer / 任务根的 uuidv7_implementer | 创建目录链barrier与target-only rename两侧fsync已修；117项通过，固定九文件patch/hash正由A03审查 |
| R01–R04 / behavior_reviewer | T01/G01/G02审查已落盘；R03发现U01恢复durability blocker；R04审T02 | 主仓库 / 任务根的 behavior_reviewer | U01新patch后复审，不把105passed当恢复安全；T02拒绝仅用history替代messages展示断言，正独立追查codec/规范；无需重复4项测试 |
| A01/A02 / architecture_reviewer | 依赖报告与U01架构复审已落盘，无新增架构blocker | 主仓库 / 任务根的 architecture_reviewer | A03审最新九文件hash、target-only fsync窗口及U02真实v4；另复核独立G03生成器pin |
| G01/G02 / gateway_user_implementer | 已集成72e755eb；新C01配置来源闭包在新worktree派单，两次429失败，暂无写入 | 新C01：out/worktrees/2026/10/04/024121-team-execution/config_logical_sources / 同名任务产物根；旧G01保留交付证据 | G01独立R02+主树10passed；C01来源规范由S02同步，待同模型恢复，不静默换模型 |
| T01–T03 / integration_fixture_implementer | T02固定单文件patch已4passed但展示合同审查未过；T03查fresh source projection归属异常 | out/worktrees/2026/10/04/024121-team-execution/integration_fixture_implementer / 任务根的 integration_fixture_implementer | T02 UTC/catalog/navigation/fork前置/typed report验证；T03不以清理当前新生成数据绕过真实缺陷 |
| S01/S02 / spec_writer | S01集成ad093e0c；S02仅同步已裁定config layer逻辑来源文档 | out/worktrees/2026/10/04/024121-team-execution/spec_writer / 任务根的 spec_writer | S01两个strict通过；3.1/3.2错误历史实现勾选已撤销，规范决定另登记，不混完成含义 |
| W01 / workspace_owner_implementer | 已授权registry→服务图→Session导航及CRUD→Gateway/bootstrap→Web双workspace闭包 | out/worktrees/2026/10/04/024121-team-execution/workspace_owner_implementer / 任务根的 workspace_owner_implementer | 基线ad093e0c，codex/workspace-session-mount；独占container/deps/main/path/session resolver，UUIDv4不改v7；固定启动mount集合/backend-owned身份CLI/单一完整service graph |
| E01 / thread_owner_implementer | 已授权8.3-E执行完整闭包与Job/pending/message真实Thread入口 | out/worktrees/2026/10/04/024121-team-execution/thread_owner_implementer / 任务根的 thread_owner_implementer | 基线ad093e0c，codex/thread-execution-owner；domain首次39passed仅类型证据，仍须SQLite/执行/stream/Web贯通 |

### 调度实测改进

| 问题证据 | 调整方式 | 后续效果 |
|---|---|---|
| 2026-10-04首轮三个agent均将陈旧共享索引的D/MM误判为源码/OpenSpec在途改动，虽共同流程已有提醒；派单同时赶上主代理文档提交，HEAD从9bc4246b变为eb9380aa | 主代理为各任务提供初始化后的独立状态索引与完整HEAD，明确生产代码未变；派单模板加入索引路径和基线落地顺序 | 三个agent均撤销误判；U01确认生产diff为0。G01/T01新派单使用稳定基线和独立worktree，尚未出现同类误判，继续观察 |
| A01调查恢复后再次询问产物与索引绝对路径，阶段结论尚未持久保存 | 主代理重发完整运行边界，要求先保存可行动的阶段依赖证据；流程增加阶段回报及恢复派单关键路径 | `dependency-review.md` 已在指定目录保存；后续恢复是否能直接读阶段证据尚待验证 |
| R01完成fixture审查后宣布写入未分配的out/tests/temp/bug_hunt_baseline目录，与原派单产物根不符 | 立即停止该路径写入，重发允许报告根与索引路径；后续审查派单显式重复绝对报告根，并要求写前比对 | 已确认只是路径计划、未越界落盘；纠正后 `t01-g01-review.md` 已落指定根，本次有效 |
| U01初稿把 `_physical.py` 从 721 扩为 953 行，新增隔离恢复状态机继续挤在已有职责中 | 重新分配同一切片的 `_quarantine.py`，physical 只保留分派、journal 复用唯一 proof 校验；避免多层异常包装 | A02确认新模块职责聚焦、无循环/方法遮蔽、physical缩减，结构修订有效；行为恢复仍有另项blocker |
| A01将“Gateway current 保留、创建拒绝”误套为 delegate child 的用户裁定 | 撤回错误推断，确认尚未写入报告；裁定记录和派单明确 owner/对象，恢复按原问题核对 | 后续报告准确区分两个owner并提出delegate完整闭包，本次纠正有效；之后由主代理按新用户授权采用前置准备 |
| T02上下文恢复后进入默认主树，误把陈旧索引MM与旧断言当作分配worktree状态，准备重做补丁 | 立即中断；独立确认主树目标等于HEAD、原worktree补丁完整且无测试遗留。恢复派单重发workdir/分支/基线/报告，并要求首次写入前核对Git顶层；证据 `coordinator/artifacts/t02-cwd-recovery-audit.json` | 未越界写入；之后正确worktree修改/复跑4passed并按指定报告交付，此次纠正有效 |
| T02恢复后又有一条只读源码检索误传主树workdir，未写代码或启动测试 | 重发唯一workdir字面值并要求后续工具调用直接使用它；不将只读事实夸大为写入事故 | 写入及测试在正确worktree，命令cwd约束尚未稳定遵守，继续观察而不宣称规则已解决 |
| S02恢复后又读取主树，误称已确认规范被并发回滚；八文件改动在分配worktree完整 | 中断并直接比较worktree文件与主线git show，重发字面cwd/分支/索引；主代理统一提供准确差异证据 | 无源码/spec越界写入；恢复核验正在进行，表明cwd规则在多个agent恢复时仍不稳定 |
| T02四项通过但移除了messages公开展示覆盖；T03同轮新数据history暴露归属写入错误 | 拒绝以当前丢字段行为为设计，独立查codec/factory/Web及canonical合同；T03生产文件从E01精确释放给单一实现者 | R04b确认display policy事实在round-trip丢失；T03锁定internal root未推进Turn，writer修复已派T04 |

本轮基线：UUIDv7标识3个文件63 passed；OpenSpec strict/all为40 passed、0 failed。T01 activity 1、history 20项通过，T02 generation最终4项通过；S01两个change strict通过。日志在coordinator与对应implementer artifacts，正式业务数据仅在out/tests对应路径。没有跑全量integration，宽source history异常仍待处理。G03所有generator按当前产物版本pin后真实gen:protocol退出0，四个生成根零差异，待独立核验提交。

现有服务环境见 [migration-state.json](../../out/tests/temp/2026/10/03/160552-workspace-migration/coordinator/artifacts/migration-state.json)：`BOXTEAM_PROJECT_ROOT` 为原路径入口，`BOXTEAM_HOME` 为该入口下 `out/development-runtime/boxteam-home`，`BOXTEAM_DEV_PORT_OFFSET=16`；命令 cwd 使用数据盘物理根。避免误查另一 unit 或重复启动服务。

每次派单、换任务、报错、审查、集成和回收后更新本节；每个活动任务登记 `编号 / agent / 已核实模型与 effort / 状态 / 基线与分支 / 独占文件 / 绝对 workdir / 产物及业务工作区 / 验证与提交 / 阻塞与回收`。无任务时不创建占位 agent。OpenSpec 台账由一个指定写入者按证据更新。

审查不通过时保留原业务断言，追到唯一权威合同与真实调用链；不能把现实现已丢失的字段直接解释为设计，也不能只凭旧测试强留兼容。数据清理授权不能用于绕过当前代码刚生成的真实缺陷。主代理统一排审查队列，subagent 只向主代理提交复审请求，避免多个发送者串改 reviewer 当前任务。

实际事故出现后修订对应条款并引用证据；已关闭的历史详情放入已有报告，本节只保留恢复所需事实，避免技能随每轮日志持续膨胀。
