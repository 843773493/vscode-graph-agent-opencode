---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择 GPT 系列；技能不能切换正在运行的主模型。

先读取[共同调度流程](memory/2026/10/07/team-collaboration-workflow.md)，继续实施时读取[实时状态](memory/2026/10/07/team-collaboration-state.md)的当前收据与队列。人数、目录、Git和派单合同只在共同流程维护；模型配置只在本入口维护，不写入 AGENTS.md。历史台账按当前 task 查询，不全文重复恢复。

创建 subagent 时显式传入：

```javascript
start_delegated_task({
  task_name,
  model: "gpt-6-luna",
  reasoning_effort: "max",
  fork_turns: "none",
  message,
});
```

模型 ID 不带 provider 前缀，不能省略或改成 newapi-local/gpt-6-luna。创建、回复及 turn_context 已通过[只读探针](../../../out/tests/temp/2026/10/03/154301-gpt-model-probe/coordinator/artifacts/model-probe.json)。工具候选列表不单独证明某 ID 不可用；真实失败记录原错误，不静默换模型。复用先确认实际模型与 effort，follow-up 不能换会话模型。

## 当前授权与效率比较

用户于2026-10-05指定一个 **gpt-6.1-sol/max** subagent贯穿实现，Luna只读审查；本轮覆盖上述默认配置，不为人数制造重复实现。实际创建与turn_context已核。主线程统一资源寻址术语，业务源码由唯一实现者修改，主串行冻结、测试和集成；已接手的caller/fixture与补救成本如实单列。

分工按实践调整：独立业务可由一个实现者贯穿；共享接口由唯一提供者先交完整合同，再推进消费者。每个相近闭包比较首个完整候选耗时、真实入口遗漏、必要测试首次通过、返工、依赖等待及主补救量；接手前投入单列。当前没有同起点可比数据，不宣布Sol速度倍率或永久最优模型。

## 已证实会严重拖慢交付的失败

- **手筛补丁遗漏生产依赖。** 从固定Git树用[校验脚本](scripts/verify_delivery.py)导出并严格重建，核blob/hash及候选外差异；签名变化一次列齐API/UI、CLI、恢复、协议、间接kwargs和fixture。残缺交付被拒说明验收有效，不证明前置检查已充分。
- **公共合同与基线错配。** live、HEAD和固定tree混读曾误派缺陷。先核完整tree、依赖和preimage；仅改文档的提交仍有完整源码tree，状态索引不代固定候选，领域同名字段不等于已删除的存储镜像。owner唯一，消费者未完成字段不阻断取得已承诺前置。审查曾将旧factory重试测试判为强覆盖，但真实bind_tools未实现且spy字段已删除；先核共同fixture/真实hook再定证据等级，不加生产alias迁就测试。移除旧接口同步清spec与全部caller，不增alias求绿。
- **helper通过，真实入口遗漏。** typed intent、mock commit和单owner绿灯曾漏admission、Provider转换、history写面及fork lineage；投影改查canonical时还漏同步checkpoint事务内调用顺序。核真实caller及其数据前置，再走producer→owner→seal/发送→restart；逆投影使用真实消息与生产归属解析，不手传已知身份替代其推导。未配对或非completed工具结果先拒绝source/seal；记录终态与执行outcome分开，unknown不能投影success。失败比较完整持久状态，不能仅比ID或几列。applied断言随唯一owner迁移，不恢复第二writer。
- **运行依赖错配与大批测试失控。** 固定生产/测试组合，tree从当次manifest取；每attempt保actual exit、raw与指纹。完整批超时不记绿，按业务组收终态；不增timeout、删原断言或迁golden来掩盖生产红灯。Web先验实际路由、认证和可见控件，再跑昂贵模型场景。
- **目录位置掩盖职责越界。** Saver/runtime曾做Provider映射，boundary模块曾直接SQL。早审实际差异、最近AGENTS、顶层依赖及真实I/O；报告标题和整包manifest不代审查范围，委托子状态不说明本文件无I/O。归入既有owner，不改指令或加shim。
- **关闭、回执与恢复被假象遮蔽。** 根PID退出不证明子进程/drain完成，超时不等于未执行或已回滚。按精确owner、revision、终态及进程组成员核实；失败保重试句柄和unknown，不以列表或默认值清账。旧生成数据可删，fresh writer缺陷仍须修复。
- **重复审查和协调扩大前置。** 先交可行动增量与未验项；复用确实审过的固定路径及未变运行依赖，只审新差异和受影响入口。主独占验收后通知一次，报告行号打磨、重复调查及状态消息不新增作者实现门槛。
- **路径/index误操作。** 每命令固定已分配绝对workdir、私有index和产物根；不/tmp、不整树复制或自建.venv。Chromium socket受路径长度限制时按共同流程使用临时短别名；先读原始错误再重试。私有index只隔离暂存，不自动限制检索；写面串行、共享index精确字节核验。只在相关任务读取共同流程中的完整提交步骤。

wire/认证、CSM/source cutoff、fork、生命周期及浏览器协议的细节，按关键词查[条件性失败细则](memory/2026/10/07/delivery-failure-rules.md)；事故和效果只在实时台账记一次，不把历史问题变成全队永久前置。反证成立及时撤单，主/审查错误及继承缺陷不归当前模型返工。

严重失败先纠正任务，再修改对应规则；后续真实交付验证后才记有效。规则无效或重复则删；只完成窄闭包不勾整个change，不结束原goal。恢复队列移除已完成动作，文档提交后的HEAD记录在外部收据，不为追逐自身坐标反复提交。
