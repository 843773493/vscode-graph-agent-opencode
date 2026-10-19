---
name: team-collaboration-gpt-hierarchical
description: 使用最多三个 Luna 协调子代理管理唯一核心 Sol 和辅助 Luna 孙代理，承接本仓库持续开发、独审和交付，减少主线程接收调度上下文。仅整理技能时不启动业务团队。
---

# GPT 分层团队协作

主模型由用户选择。主线程接收用户指令、确定范围、处理协调层无法解决的裁决；交付协调层持续推进已授权任务。仅准备技能不授权继续历史实施，用户要求在某个功能边界停止时，不自动接下一闭包。

## 团队结构

| 层级 | 模型与数量 | 责任 |
| --- | --- | --- |
| 主线程 | 用户选择的模型 | 用户沟通、范围、必要裁决；平时等待协调结果 |
| 交付协调 child | `gpt-6-luna` / `max`，1 名 | 唯一业务协调负责人：派单、依赖、写租约、运行窗口、证据、固定候选与验收；Git 集成可分配给本轮唯一指定的集成者 |
| 独审协调 child | `gpt-6-luna` / `max`，按需 1 名 | 前移消费者核查、安排未参与写入者审查、直接同交付协调者解决 finding |
| 辅助协调 child | `gpt-6-luna` / `max`，按需 1 名 | 隔离小修、命名核查及其验证；交准确结果后停止，不接管核心 |
| 核心 grandchild | `gpt-6.1-sol` / `max`，全队唯一 | 贯穿完整核心场景，同批迁齐声明、权威写入、查询、恢复、DTO、消费者和测试 |
| 辅助 grandchildren | `gpt-6-luna` / `max` | 消费者及测试前提核查、独审、外围小修，并自行完成相关验证 |

主线程最多直接管理 3 个 child；全部后代最多 15 个。业务协调者与唯一 Git 集成者可以分开，但集成职责须由本轮明确指定的既有成员承担，不额外突破直属 child 上限；作者和审查者不写 Git index。长期实施至少保持 3 名有明确授权工作的 Luna，包含协调者；收尾按实际工作留人，不为凑人数扩大范围。只有交付协调者创建或续派唯一核心 Sol。child 可管理 grandchildren，孙层不继续派生。

切换旧团队时先结束旧写租约和在途运行；旧直系 Sol 停写交接后，再创建新的核心 grandchild。主线程不直接管理孙代理的日常实现或报告；真实阻塞需要主介入时，限定范围，处理后归还协调层。

## 派单与接续

创建代理使用当前创建工具，明确传入：

~~~javascript
start_delegated_task({
  task_name,
  model: "gpt-6-luna", // 唯一核心 grandchild 使用 "gpt-6.1-sol"
  reasoning_effort: "max",
  fork_turns: "none",
  message,
});
~~~

模型 ID 不带 provider 前缀，不省略、不静默替换。候选列表不能单独证明不可用；实际调用失败保留原错误，明确受影响工作。后续派单不能改变成员原模型。

交付协调者在本轮产物目录维护一份简短的当前接续记录：自己的 canonical ID 和职责、当前用户边界、成员 canonical ID/父级/角色/模型、唯一 Sol、写租约、在途命令、验收门及下一可执行动作。阶段变化即更新，不把旧流水当当前分配表。接续先读这份当前记录；上下文压缩或最后一条局部请求不重置既有职责与未变授权。成员 ID 必须来自本轮工具返回，续派目标须属于预定父级，不能按同名旧角色推测路径。

completed 成员用 continue/followup 唤醒；send/deliver 只补运行中任务。收到 429 直接向原成员续发“继续”，不额外查状态、进程或模型，不创建替代者。其它错误据原件判断；持续不可用时报告影响，不无限重试或自行改环境配置。

派单传本任务所需目标、真实根目录、必要合同入口、写面、完整验收场景、运行窗口/产物路径及直属接收者，不复制主线程长对话或全部历史。协调者在授权内自行续排依赖与返工、唤醒协作协调者；自己准备接线，不反向要求主线程逐项派单或重复授权。

## 实施与审查怎样衔接

Sol 改完整核心场景；Luna 可在作者实施期只读当前源码与真实入口的消费者闭包（含 SQL 列、loader、恢复及测试 setup），将注明所读源码版本的观察交直属协调者收敛并与交付协调者、Sol 协同。按现有授权做源码只读核查时，读前后取 SHA 用来限定观察版本，不是审批门；授权未变时无需为此另向主线程申请许可。隔离小修由 Luna 完成自己的相关验证，不交 Sol 补救。公共合同只设一个写入 owner。

一次授权覆盖该任务的必要实现、接线和受保护验证。协调者分配运行窗口后，作者可以连续完成临时诊断脚本、实际输入固定、运行与有证据的接线修正；不逐个脚本或 profile 设置 STOP→转交→二次授权。优先复用真实入口及有效 fixture，不为诊断自造另一套业务前提。临时观测只证明它实际覆盖的行为，人为锁竞争不等于复现产品持锁故障。

整批迁齐后集中静态检查、少量真实完整入口与受影响回归，保留已约定的验收门。候选交独审时冻结本批写面，锁定唯一正式 manifest 与固定 Git 树并严格重建。持续 goal 中，协调者须在冻结前准备下一具体未闭场景及写面，交出候选后立即续派唯一 Sol；冻结本批不等于让作者等待独审或 Git 集成。下一改动若依赖或涉及冻结源码，由协调者合并完整合同并更新候选，不为并行制造无关任务；用户指定的停止边界仍优先。

未参与写入者基于固定候选的 manifest 和 Git tree 核完整调用链，GO 同时记录结论所依赖的源码 blob。最终提交 tree 仍按最终 manifest 机械核对。parent commit 或无关文档变化本身不撤销相关源码审查结论：依赖 blob 未变时复用 GO，只审新增或变化的相关依赖；依赖 blob 改变时补审受影响部分。审查疑点先查真实 caller 和错误路径，再决定是否阻断；附加标签、报告形式或尚无具体错误路径的精度猜测不自动成为新验收门。复用结论不放宽身份、顺序、冲突或错误归属断言。

Git index 的创建、写入与状态变更由本轮唯一集成者负责，并使用绝对私有 `GIT_INDEX_FILE` 与 `GIT_OPTIONAL_LOCKS=0`，保留共享暂存。作者和审查者不建或写 Git index；审查者在固定候选审查中可设置 `GIT_OPTIONAL_LOCKS=0`，只读查询指定不可变 Git 对象（`rev-parse`、`show`、`cat-file`、`ls-tree`），无需建索引。只有本轮唯一集成者在用户已授权时执行普通提交与 push，并核提交树和远端 SHA；不 force、不改写历史。上游审查失败不能记作 GO；必要替代审查须保持作者独立性，并明确记录替代范围。

集成者调用 [run_git.py](../team-collaboration-gpt/scripts/run_git.py) 自动接好私有索引；直接运行 `commit` 子命令时，入口先检查私有索引中的 Python blob，`--task-dir` 使用本批产物绝对路径：

~~~bash
<ROOT>/.venv/bin/python <ROOT>/.codex/skills/team-collaboration-gpt/scripts/run_git.py \
  --task-dir <ROOT>/out/tests/temp/YYYY/MM/DD/<run_id>/<role>/artifacts -- status --short
~~~

同批 Git 命令复用该 task-dir；新批次使用新目录。入口只在索引不存在时从 HEAD 初始化，不清空已有私有暂存。不支持以 `-` 开头的 Git 全局选项前缀（例如 `-C <repo>`）；必须直接提供 Git 子命令，避免全局前缀绕过提交检查。命令向量首项为 `commit` 时，入口拒绝 `-a/--all`、`-i/--include`、`--interactive`、`-o/--only`、`-p/--patch`、pathspec 选项及位置路径，再从私有索引读取相对 HEAD 变动的 Python blob，先做 AST 解析，再用 Ruff 检查 F821/F823；任一失败都会阻止该直接提交调用。提交变动 Python 文件时环境须能调用 Ruff CLI。该检查不代替最终树核验或业务验收。

## 路径与运行证据

默认共用当前真实工作树，不复制整仓或新建临时工作树。已有授权确需 worktree 时，只放 `out/worktrees/YYYY/MM/DD/<run_id>/<task>/`，日期按北京时间冻结。

临时控制和原件使用 `out/tests/temp/YYYY/MM/DD/<run_id>/<role>/`，隔离业务工作区用 `workspace/`，产物用 `artifacts/`。正式测试沿用 `out/tests/<与 tests 同名路径>/`。不使用 `/tmp`、项目根目录测试数据或历史临时业务工作区。正式输出将被覆盖时，优先同盘重命名封存原件，记录路径；不要复制整套旧业务数据或把旧件算成新结果。

复用 [run_task.py](../team-collaboration-gpt/scripts/run_task.py)，协调者准备准确根路径和有效 profile，作者在分配窗口内运行：

~~~bash
<ROOT>/.venv/bin/python <ROOT>/.codex/skills/team-collaboration-gpt/scripts/run_task.py \
  --profile <绝对profile> --attempt <唯一名> --seconds <预算> -- <原命令及参数>
~~~

测试和执行性诊断带进程外超时、内存保护；profile 只绑定实际落盘输入，变更新建版本，不切在途输入。保留原始输出、实际退出码、源码指纹及未完成阶段，不覆盖或搬抄结果。有界源码读取不逐次建 attempt。按需使用 [verify_run.py](../team-collaboration-gpt/scripts/verify_run.py) 和 [verify_delivery.py](../team-collaboration-gpt/scripts/verify_delivery.py)；机械核验不代替业务验收。

## 主线程怎样等待

孙代理只向直属 child 交付；child 之间直接处理 finding、依赖、唤醒和返工。持续 goal 未闭且仍有可执行任务时，交付协调者用消息汇报阶段结果后继续推进，不以局部切片完成的 FINAL 结束委派、等待主线程重新唤醒。主线程只收可交付里程碑、协调层无法解决的真实阻塞、需要用户裁决的事项，或用户询问时的精简状态；内部接线准备不逐条转报。

一次汇总（含最终回复）不超过 8 行：范围/候选、已证行为、检查实际结果、重要未闭项、下一动作及准确原件路径。启动、PID、profile 准备和分步 lint 留在接续记录，不逐项上报；不抄送孙代理原报告、日志或整份 diff。主线程只按疑点抽核，不重复全部核验。

派单后使用等待工具让出执行，新用户指令交对应 child。观察期限确已超出时向负责人问一次阻塞与下一动作；不短周期轮询、因安静重派作者或展开旧全树。交付协调者收尾报告提交/远端 SHA、证据入口和未闭项；用户指定边界完成后停止扩展，等待新指令。

## 按实践维护

只保留能改变后续决策且可追溯的结论；详细过程按需读本轮原件，不默认加载旧流水。当前已观察的失败及处理如下，均不证明整体提速：

- **筛选与有效证据**：RUN20 把 node ID 放进 `PYTEST_ADDOPTS`，与 matrix 自带的 `tests/integration` 合并成全套误跑；改用 `-k` 后实际选中 1 项、1,795 项取消选择，50.73 秒通过。该受保护 matrix 结果有效，曾因台账指定另一 runner 而安排的重复运行已撤回。精确 node ID 可走现有 `run_task.py` profile；判断范围看实际选中项，判断证据看源码依赖与行为，不以 runner 名称再设门。原件：`out/tests/integration/backend/agents/test_resource_navigation/artifacts/codex-verify-navigation-20261019{,-target1}.{stdout.log,stderr.log,exit-code.txt}`。历史漏筛选与 collection 失败原件仍见 `out/tests/temp/2026/10/18/fork60-deadline/fork60_closeout/artifacts/python/`。
- **资源保护接线**：RUN20 metadata attempt003 绕过已有入口另写 4 GiB shell 包装，matrix 无法设置自身 8 GiB 上限，pytest 前 exit 97，重现了 RUN16 的失败。attempt004 改用现有 `run_task.py` profile 和直接单 suite matrix 命令，保留外部 300 秒超时，由 matrix 设置内存上限；实际 1 passed / 1,799 deselected，46.19 秒，源码前后相同且无残留进程。入口已有该分工，不再另套降低 hard limit 的包装；这只证明本次接线与 metadata 门通过，不证明整体提速。原件：`out/tests/temp/2026/10/19/goal-delivery-owner20/root-skill/artifacts/matrix-memory-owner-003/evidence.json`；RUN16 旧失败仍见 `out/tests/temp/2026/10/18/hierarchical-goal-resume-16/goal_delivery_run15/artifacts/runs/core-current-sha-integration-001/`。
- **候选与提交不一致**：RUN18 工作树 GO 未挡住最终提交中的语法错误及 F821/F823，故提交入口检查实际私有索引 blob，并拒绝绕过索引的工作树提交参数。本轮 core22 又曾把共享旧 stage 相对新 HEAD 的差异误判为本轮 index 写入并暂停交付；root 核 `.git/index` 前后 SHA 均为 `6af3f3ad6b0c8c9b49e9fb3e9997ed47ba787da391905c406a91276f2540e18a`，与本轮交付收据中的 before/after 一致后撤销暂停，沿原 private index 继续。相对 HEAD 的 diff 不能证明 index 写入；用已有前后 SHA 判定，保留共享 stage，不增审批或重复全树扫描。原件：`out/tests/temp/2026/10/18/fork60-fixture-closeout-18/coordinator/artifacts/` 中的 `fork60-final-candidate.patch`、`fork60-independent-review.md` 与 `runs/commit-tree-bce3fbf-final-002/`；本轮交付收据：`out/tests/temp/2026/10/19/integration-queue-owner23/team-writer/artifacts/root-binding-delivery-001/run-001/delivery-receipt.json`。这是纠正一次暂停，不代表整体提速。
- **接续职责与实际交付**：RUN20/21 曾出现局部 GO 后停派、候选漏项或混入非本批文件及分批补交；本轮看到 completed 写者在收到 `continue` 前有空档，集成队列称“续派”时写者仍未收到具体任务。CIL010 将原 17 路与 v3 的 8 路按路径合并去重为完整 19 路候选，commit `4050ef02ac04aba1c530059ba4c73f3797a13334` 与 `postpush-remote` 的远端 SHA 一致。证据：`out/tests/temp/2026/10/19/goal-delivery-owner21/metadata-integrator-owner21/artifacts/cil010-composite-001-integration-001/commit-verified.json` 和同目录 `receipts/postpush-remote/`。本轮交付可核实，但仍未证明整体提速。
- **GO 与最终 tree 的边界**：本轮 queue23 曾因先提交无关 STATE/current-entry 文档会改变 JCS parent tree，提出重绑与重审；root 裁定仅按相关源码依赖 blob 的变化补审。queue23 接受后独立提交并推送文档 `e1347658747dd0e83e2bb9bdcc97c51123ccd74d`，沿用源码 GO，并保留最终 tree 核验。此记录只覆盖一次协调修正，不证明整体提速。原件：queue23 当前记录 `out/tests/temp/2026/10/19/integration-queue-owner23/coordinator/artifacts/CURRENT`；writer commit、parent、tree 与远端核对见 `out/tests/temp/2026/10/19/integration-queue-owner23/team-writer/artifacts/state-current-entry-docs-001/commands/{023-commit-sha,024-commit-parent,025-commit-tree,030-postpush-remote}/`。
- **共享 fixture 与旧结果**：RUN19 固定 SSH 端口冲突、旧源码 SHA 的 setup error 曾被混作业务失败；正式 fixture 生命周期需独占运行窗口，修复后的结论不能由旧输入结果覆盖。原件位于同一 RUN19 根的 `core_sol/artifacts/runs/same-owner-repro-001/`、`core_sol/artifacts/runs/uri-transfer-and-run15-locks-001/` 及 `sshd-identity-luna/artifacts/attempt-{003,005,007}/`。

上面未写完整路径的 RUN19 原件，均相对 `out/tests/temp/2026/10/19/goal-core-resume-19/`。
