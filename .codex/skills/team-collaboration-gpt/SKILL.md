---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择 GPT 系列；技能不能切换正在运行的主模型。

先读取[共同调度流程](memory/2026/10/08/team-collaboration-workflow.md)，继续实施时读取[实时状态](memory/2026/10/08/team-collaboration-state.md)的当前收据与队列。人数、目录、Git和派单合同只在共同流程维护；模型配置只在本入口维护，不写入 AGENTS.md。历史台账按当前 task 查询，不全文重复恢复。

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

本轮按用户授权，由一个 **gpt-6.1-sol/max** subagent贯穿实现，Luna只读审查；覆盖上述默认配置，不为人数制造重复实现。实际创建与turn_context已核。Sol独占ROOT当前业务闭包的源码与受保护测试；主线程维护规范/技能、独立审查并最终串行冻结集成，不并发改业务源码或占用作者的正式测试工作区。保护路径与其他owner独占面仍有效；已接手caller/fixture和主补救成本单列。

以当前已授权的完整调用闭包为一批，一次迁齐全仓必要源码、调用方、DTO/schema和核心所需公共fixture；编辑时做快速静态分析，批末统一复核。先用少量现有真实入口修通共同装配与核心业务，再集中迁移其余unit/边界fixture并补回归，不能把全历史测试迁齐设为核心准入门。同一公共前置挡住多个用例时先集中修正，定向确认能通过该前置后再展开整组，不让每例重复等待同一错误。已绿且依赖未变的检查复用。实现中不逐小包冻结、不逐文件申请授权、不每改一点就跑动态测试；同一作者完成整批并最终停写后，冻结完整候选供审查集成；独审发现真实缺陷时修正受影响闭包，保留原候选并固定修订完整树，只增审新增差异。测试始终带进程外保护，最终必要验收保留。

分工按实践调整：独立业务可由一个实现者贯穿；共享接口由唯一提供者先交完整合同，再推进消费者。每个相近闭包比较首个完整候选耗时、真实入口遗漏、必要测试首次通过、返工、依赖等待及主补救量；接手前投入单列。当前没有同起点可比数据，不宣布Sol速度倍率或永久最优模型。

## 严重失败与纠正

只记录影响调度决定的原因和修正；attempt、路径、退出码及验证范围归实时台账。

- **小包调度阻断完整实现。** 反复扩租约、冻结、同步与重跑造成依赖等待。一次授权整个调用闭包，先迁齐生产和核心公共fixture，再核心修通、集中边界，最终只冻结一次。A01完整包已交付，生产净减1109行；尚无同起点模型效率对比。
- **手工补丁漏调用方。** 补丁字节一致仍曾漏fork/import消费者。停写后从固定Git树机械导出、严格重建并核全仓caller；公共签名同时迁间接参数、fixture及子进程程序字符串，不能用别名恢复旧链。fixture迁移后全文件复核重复的旧定位断言，只改helper曾造成连续重跑。
- **多作者同时改合同造成漂移。** 公共接口由唯一owner完成，再推进消费者；区分HEAD、固定候选和live。同一候选已过检查按实际依赖复用，只审新增差异和受影响入口。已被真实接线推翻的旧诊断立即撤单。
- **helper绿灯漏真实装配与输入形态。** 默认Skill producer、internal scope/provider冻结、child准入曾到HTTP阶段才暴露；简化Provider桩又漏vendor消息ID和受保护reasoning。少量核心先贯穿真实准入→Provider→工具→下一请求→恢复，保真实carrier。模型身份绑定本次sealed callback并校冲突；checkpoint/history共用原已知schema解码，未知格式仍明确拒绝，不靠lc_run--、删reasoning或改模型配置求绿。启动失败先读正式stderr，Gateway展示ID不能冒充backend UUID。桩只取最后消息的断言失败不能证明完整请求丢来源；独立Saver冷恢复与真实backend重启分别验收。幂等取写入owner的权威事实，不能用投影中已有message判断控制更新已完成；此早返曾漏首次checkpoint与stale task清理。生成结果的API脱敏已绿，固定审查仍发现控制ID落入canonical annotations、child漏typed provenance；核心同时核producer、持久payload/extensions和冷投影，展示脱敏不能代替存储合同。本次修复的真实核心与直接边界已核canonical/extension、typed来源和冷投影，验证了这项窄纠正；不外推其它producer。
- **fork/rebase只复制可展示内容，丢私有上下文。** H4漏request-only seed，再漏selection/ref目标身份与未发布目标定位。一次联核冻结contribution、selection、detail、prefix和target-local映射，creation record仅用于原受控创建事务；不从当前seed重读，不用fake Turn/notice或提前发布补洞。fullcopy又漏prefix/root来源映射；不能只看ordinary fork便决定全保留别名，须核各模式draft/import合同，所有冻结载体共用对应owner映射。H4/H5及root/tail三模式6例已通过，集中边界仍待验。
- **锁内重入owner令整轮超时。** Goal callback再次get同owner、Terminal持Session gate再提交source均发生自锁。锁内冻结对象直接传递；跨owner采用原稳定event/generation的短claim→释放→原事务提交→幂等finish，typed身份贯通transport。H2真实核心已修通，失败finish与恢复待验。
- **前置错误阻断业务重试，外层failed掩盖内层unknown。** H3空响应被路由提前抛掉、question恢复漏受信字段，成功retry又漏早期assembly收尾。先修真实协议/恢复的公共前置，联核Job、execution、每次model-call与assembly，不只收最新一次。H6硬杀暴露已sealed assembly对应model-call尚未提交，NULL联查结果阻断启动；按execution_lost关闭该assembly、保已提交model事实后真实冷启动通过。区分已确认业务失败和未确认调用，不补造Provider响应或一律覆盖历史结果。
- **重复跑同一前置，结果丢失或虚绿。** 同一公共fixture曾挡23例，cProfile外层0曾包着pytest失败，H1覆盖raw又丢早红。先用少量用例穿过公共准入再展开；每attempt独占raw/退出码/产物。保护超时、截断日志与缺失raw不计通过，业务timeout不为求绿放宽。先核首次实际副作用：quota先拒绝、fallback再失败时按原错分类，旧错误未出现不证明越过修复；外部前置未满足时停重复并推进独立部分。Web消费迁移把重建的workspaces数组当effect依赖曾循环请求至内存保护退出；按选中目标的workspace/backend/connection稳定身份触发，并断言解析次数，相关41例已通过。
- **批量迁移失败后扩大写入。** AST字节偏移错误留下空路径，Ruff无参数扫全仓。迁移非零即停止；静态修复只接收非空且属于闭包的路径数组，AST按UTF-8字节定位。范围外68文件已独核恢复，预防措施后续效果待验。
- **路径与索引偏离仍发生。** 每命令固定物理cwd、私有index和out产物根，不/tmp、不整树复制。重复违规的只读审查会话撤Git权限，仅读指定文件；共享index字节变动但stage不变，不据此认定源码吞改，也不恢复旧index。当前纠正效果待验。
- **本地正文加密增加恢复和测试链路。** 用户取消同目录AES、两个key及thread HMAC，A01已删除；保单份detail、完整性和默认脱敏。联邦、SSH、认证及Provider opaque reasoning仍归原owner，不迁旧开发数据。

具体技术失败按关键词查询[条件性细则](memory/2026/10/08/delivery-failure-rules.md)，当前结果与待完成项只在[实时台账](memory/2026/10/08/team-collaboration-state.md)维护。严重失败先纠正派单，再窄改规则；后续真实交付验证后才记有效，无效或重复规则删掉。历史事故不自动成为全队前置；窄闭包通过不勾整个 change，不结束原 goal。文档提交后的 HEAD 只记外部收据，不追逐自身坐标反复提交。
