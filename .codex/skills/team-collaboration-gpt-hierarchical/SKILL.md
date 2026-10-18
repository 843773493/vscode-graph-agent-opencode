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
| 交付协调 child | `gpt-6-luna` / `max`，1 名 | 唯一交付负责人：派单、依赖、写租约、运行窗口、证据、固定候选、串行集成 |
| 独审协调 child | `gpt-6-luna` / `max`，按需 1 名 | 前移消费者核查、安排未参与写入者审查、直接同交付协调者解决 finding |
| 辅助协调 child | `gpt-6-luna` / `max`，按需 1 名 | 隔离小修、命名核查及其验证；交准确结果后停止，不接管核心 |
| 核心 grandchild | `gpt-6.1-sol` / `max`，全队唯一 | 贯穿完整核心场景，同批迁齐声明、权威写入、查询、恢复、DTO、消费者和测试 |
| 辅助 grandchildren | `gpt-6-luna` / `max` | 消费者及测试前提核查、独审、外围小修，并自行完成相关验证 |

主线程最多直接管理 3 个 child；全部后代最多 15 个。长期实施至少保持 3 名有明确授权工作的 Luna，包含协调者；收尾按实际工作留人，不为凑人数扩大范围。只有交付协调者创建或续派唯一核心 Sol。child 可管理 grandchildren，孙层不继续派生。

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

交付协调者在本轮产物目录维护一份简短的当前接续记录：自己的 canonical ID 和职责、当前用户边界、成员 canonical ID/父级/角色/模型、唯一 Sol、写租约、在途命令、验收门及下一可执行动作。阶段变化即更新，不把旧流水当当前分配表。接续先读这份当前记录；最近一条局部请求不能替代整轮职责。成员 ID 必须来自本轮工具返回，续派目标须属于预定父级，不能按同名旧角色推测路径。

completed 成员用 continue/followup 唤醒；send/deliver 只补运行中任务。收到 429 直接向原成员续发“继续”，不额外查状态、进程或模型，不创建替代者。其它错误据原件判断；持续不可用时报告影响，不无限重试或自行改环境配置。

派单传本任务所需目标、真实根目录、必要合同入口、写面、完整验收场景、运行窗口/产物路径及直属接收者，不复制主线程长对话或全部历史。协调者在授权内自行续排依赖与返工、唤醒协作协调者；自己准备接线，不反向要求主线程逐项派单或重复授权。

## 实施与审查怎样衔接

Sol 改完整核心场景；Luna 可在作者实施期只读当前源码与真实入口的消费者闭包（含 SQL 列、loader、恢复及测试 setup），将注明所读源码版本的观察交直属协调者收敛并与交付协调者、Sol 协同。隔离小修由 Luna 完成自己的相关验证，不交 Sol 补救。公共合同只设一个写入 owner。

一次授权覆盖该任务的必要实现、接线和受保护验证。协调者分配运行窗口后，作者可以连续完成临时诊断脚本、实际输入固定、运行与有证据的接线修正；不逐个脚本或 profile 设置 STOP→转交→二次授权。优先复用真实入口及有效 fixture，不为诊断自造另一套业务前提。临时观测只证明它实际覆盖的行为，人为锁竞争不等于复现产品持锁故障。

整批迁齐后集中静态检查、少量真实完整入口与受影响回归，保留已约定的验收门。最终产品候选才集中 STOP，锁定唯一正式 manifest 与固定 Git 树并严格重建；未参与写入者基于该候选核完整调用链及证据，GO 仅绑定这份 manifest 与该树。审查疑点先查真实 caller 和错误路径，再决定是否阻断；附加标签、报告形式或尚无具体错误路径的精度猜测不自动成为新验收门。已通过项按真实源码依赖复用，只审新增差异，不放宽身份、顺序、冲突或错误归属断言。

Git 只交本轮唯一集成者；每个 Git 子进程使用绝对私有 `GIT_INDEX_FILE` 与 `GIT_OPTIONAL_LOCKS=0`，保留共享暂存。禁 Git 的作者和审查者不建索引。用户已授权的普通提交与 push 可直接执行，核提交树和远端 SHA；不 force、不改写历史。上游审查失败不能记作 GO；必要替代审查须保持作者独立性，并明确记录替代范围。

集成者调用 [run_git.py](../team-collaboration-gpt/scripts/run_git.py) 自动接好私有索引，`--task-dir` 使用本批产物绝对路径：

~~~bash
<ROOT>/.venv/bin/python <ROOT>/.codex/skills/team-collaboration-gpt/scripts/run_git.py \
  --task-dir <ROOT>/out/tests/temp/YYYY/MM/DD/<run_id>/<role>/artifacts -- status --short
~~~

同批 Git 命令复用该 task-dir；新批次使用新目录。入口只在索引不存在时从 HEAD 初始化，不清空已有私有暂存；它不代替最终树核验或扩大 Git 授权。

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

孙代理只向直属 child 交付；child 之间直接处理 finding、依赖、唤醒和返工。主线程只收可交付里程碑、协调层无法解决的真实阻塞、需要用户裁决的事项，或用户询问时的精简状态；内部接线准备不逐条转报。

一次汇总（含最终回复）不超过 8 行：范围/候选、已证行为、检查实际结果、重要未闭项、下一动作及准确原件路径。启动、PID、profile 准备和分步 lint 留在接续记录，不逐项上报；不抄送孙代理原报告、日志或整份 diff。主线程只按疑点抽核，不重复全部核验。

派单后使用等待工具让出执行，新用户指令交对应 child。观察期限确已超出时向负责人问一次阻塞与下一动作；不短周期轮询、因安静重派作者或展开旧全树。交付协调者收尾报告提交/远端 SHA、证据入口和未闭项；用户指定边界完成后停止扩展，等待新指令。

## 按实践维护

只保留能改变后续决策且可追溯原件的实践结论，过程记录留在本轮 artifacts。

- 2026-10-18，未设 `PYTEST_ADDOPTS` 启动 Python integration matrix，收集了全部 1,716 项后由负责人停止（143）；精确选择 6 个 fork 用例后 6 项通过。原件：`out/tests/temp/2026/10/18/fork60-deadline/fork60_closeout/artifacts/python/matrix.*`、`out/tests/temp/2026/10/18/fork60-deadline/fork60_closeout/artifacts/python/attempt-003/`。
- 同日，外层 `ulimit -d 4194304` 因当前 shell 无权调整限制而在 pytest 启动前失败（97）；移除外层限制后由 matrix 管理资源，真实 API 用例 1 项通过。原件：`out/tests/temp/2026/10/18/fork60-deadline/fork60_delivery/artifacts/runs/python-real-fork-001/`、`out/tests/temp/2026/10/18/fork60-deadline/fork60_delivery/artifacts/runs/python-real-fork-003/`。

角色或阶段漂移的根因，以及分层协作对 token、耗时或成本的收益，均未获证据。
