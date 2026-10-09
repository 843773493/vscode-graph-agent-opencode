---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择 GPT 系列；技能不能切换正在运行的主模型。先读[共同流程](memory/2026/10/09/team-collaboration-workflow.md)；恢复实施时读[实时台账](memory/2026/10/09/team-collaboration-state.md)的当前租约、收据与队列，不全文恢复历史。人数、路径、Git与派单合同只在共同流程维护，AGENTS.md不绑定模型或调度策略。

创建subagent显式传入：

```javascript
start_delegated_task({
  task_name,
  model: "gpt-6-luna",
  reasoning_effort: "max",
  fork_turns: "none",
  message,
});
```

ID不带provider前缀，不省略或换成newapi-local/gpt-6-luna。创建、回复和turn_context已有[真实探针](../../../out/tests/temp/2026/10/03/154301-gpt-model-probe/coordinator/artifacts/model-probe.json)。工具候选列表不能单独证明不可用；真实创建失败则报告原错误，不静默换模型。复用前确认实际模型/effort，follow-up不能换会话模型。

## 当前授权

本轮用户指定一个 **gpt-6.1-sol/max** 贯穿核心，**gpt-6-luna/max** 只做隔离小修、独审和验收，覆盖默认组合。Sol独占ROOT核心；主代理维护规范、技能、机械固定与串行集成，不手写业务源码。Luna写入前核Sol租约与真实依赖，只给明确的小范围代码worktree，不交大型/核心重构。37 mixin与平行状态表只有结构风险，不能据此派大型改造；无具体finding就收尾，不为并发人数制造任务。

同一完整调用闭包一次迁齐生产源码、调用方、DTO/协议和必要fixture；编辑后快速静态，批末统一Webbuild。窄测试消费者由作者在同批内完成有限的真实入口调试，再STOP交独立验收；不把每次定位或接线修订都冻结后让审查者代跑，避免交接本身放大返工。先少量真实核心入口，集中修共同前置，再补受影响边界；最后整体STOP、固定、独审和集成。已有通过项按实际依赖复用，不逐微包冻结、重复未变检查或重写历史报告。未实现的生产入口不能藏入“最终验收”，新增诊断不能升级为原需求以外的阻断门。

公共接口由唯一owner固定后再推进其它作者的消费者；单作者接口与消费者同批完成，不人为设中间门。实际效果按完整候选耗时、入口遗漏、首次通过、返工与依赖等待比较，继承投入及主代理补救单列。没有同起点A/B，不宣布模型速度倍率或永久最优。

## 严重失败的执行纠正

具名证据、技术条件及后续效果查[失败细则](memory/2026/10/09/delivery-failure-rules.md)，只按当前任务关键词读取。

- **残缺交付。** 禁Git作者只交STOP源码/SHA与真实raw。主从固定Git树导完整补丁、严格重建并核全部caller；完整diff-check显式指定本批baseline/candidate、包含新增文件。对象参数从本批manifest取。最终报告STOP后保存字节/SHA，新增finding另存版本；只集成固定blob，不add作者live、裁hunk或用陈旧index提交。
- **helper绿而真实入口漏接。** 核真实装配、准入、持久提交和重开读取；替身只放外部传输边界。性能断言覆盖声称消除的工作量，只计manifest或页DTO会漏掉轻量全表fetchall/排序；不附加不可能的SQLite总计数O(1)门。公共纯合同放低层既有owner，避免core反向导入编排。跨owner字段、namespace、事务连接和历史cut随完整调用链传递；锁内回读复用当前writer连接，不重取同锁。不新增同义状态表、默认假值或兼容层来遮缺口。
- **异常或清理虚假成功。** 保原异常和cause；取消必须传播，独立资源保持close-all/join-all并发回收。冷history/detail读取不初始化或repair，合法空历史无writer副作用，损坏权威index明确失败并保正文。清理失败不能先删权威登记。具体owner合同见细则，不据一次小修扩大为整体生命周期通过。
- **路径漂移。** 每写命令显式物理workdir，脚本断言cwd和目标resolve属于租约根；前一条shell的cwd不约束下一工具。路径缺失回报主代理，不自建fallback。越界保现场，只撤回确证片段，保留字节在正确根继续；重复漂移收回写入/测试/Git权限后转交。主提供完整可执行测试命令；复用ROOT venv须显式UV_PROJECT_ENVIRONMENT、--no-sync及唯一PYTHONPATH，禁止/tmp、新venv、整仓复制或源码根业务工作区。
- **测试消费者漏迁或过窄。** fixture与真实生产合同同批迁移；数据库索引引入连接依赖时，盘点直接SQL及损坏/恢复夹具，不能只迁owner内测试。量化先每类一个真实控件样本核目标、请求、正式回执ID、持久结果和UI，再展开矩阵；最小模式减少操作样本，保留会触发虚拟化等真实运行分支的夹具规模。虚拟列表DOM count不代表目录折叠或资源不存在，按实际控件展开态和滚动定位，保留初始真实加载屏障；虚拟行可在定位后脱挂，视口计算须核真实owner并有界重取，不能把空祖先误报产品失败；浏览器evaluate闭包只用浏览器能力和显式序列化入参，Transpiler绿不能证明Node变量可在页面运行；202用同ID完成合同。multipart在实际HTTP入口取body、用原生解析器并按ID配对，不倒填期望事实；媒体类型按语义判定。响应URL可为相对值，以冻结请求URL归一，切换诊断目标后核共享callee结果消费。计时行未移动不等于口径未变，核实际起止事件，视口准备与额外诊断放在原统计窗口外。原样本量与性能门控保持，装配/诊断失败不作性能结论。
- **证据失真与无效重验。** 每attempt独立raw、actual退出码、耗时和程序计算SHA，wrapper0或PASS文字不能推翻测试非零退出。边界组用具名节点；失败后只补受影响入口，不重跑整组。Provider quota等外部同因不重复求绿、不擅改环境；integration不能冒充产品E2E。恢复工具的JSON字符串先解码，只输出具名限长事件。

发现严重失败先纠正派单，再窄改相应规则；真实后续交付验证后才记有效，无效或重复规则删掉。技能入口不积累日志，当前状态只在实时台账维护。窄闭包通过不勾整个change或结束原goal。
