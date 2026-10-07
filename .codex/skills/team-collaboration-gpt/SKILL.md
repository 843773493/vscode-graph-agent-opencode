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

本轮按用户授权，由一个 **gpt-6.1-sol/max** subagent贯穿实现，Luna只读审查；覆盖上述默认配置，不为人数制造重复实现。实际创建与turn_context已核。Sol独占ROOT当前业务闭包的源码与受保护测试；主线程维护规范/技能、独立审查并最终串行冻结集成，不并发改业务源码或占用作者的正式测试工作区。保护路径与其他owner独占面仍有效；已接手caller/fixture和主补救成本单列。

以当前已授权的完整调用闭包为一批，一次迁齐全仓必要源码、调用方、DTO/schema和公共fixture，批末统一静态分析。先用少量现有真实入口修通共同装配与核心业务，再补边界/回归；同一公共前置挡住多个用例时先集中修正，定向确认能通过该前置后再展开整组，不让每例重复等待同一错误。已绿且依赖未变的检查复用。实现中不逐小包冻结、不逐文件申请授权、不每改一点就测；同一作者完成整批并最终停写后，只冻结一次完整候选供审查集成。测试始终带进程外保护，最终必要验收保留。

分工按实践调整：独立业务可由一个实现者贯穿；共享接口由唯一提供者先交完整合同，再推进消费者。每个相近闭包比较首个完整候选耗时、真实入口遗漏、必要测试首次通过、返工、依赖等待及主补救量；接手前投入单列。当前没有同起点可比数据，不宣布Sol速度倍率或永久最优模型。

## 严重失败与纠正

- **小包调度吞掉实现时间。** 主线程反复扩租约、冻结、同步和重跑，阻断完整消费者交付。一次授权整个调用闭包，让同一作者实现并带保护测试；只在最终停写后冻结完整候选。已确认共同前置及剩余公共fixture集中迁齐，避免每改几份就展开大组；不为统一取红指纹等齐全矩阵，作者按重复失败和耗时选择修/测顺序，每组依赖稳定即可。核心已通过，整批效率待最终交付验证。
- **交付遗漏调用方。** 静态绿灯和补丁字节一致仍曾遗漏删除模块的 fork/import caller。固定树机械导出并严格重建，公共签名迁移核全仓调用方、公共fixture、间接参数和子进程程序字符串，直接AST调用扫描不能证明闭包完整；不能靠别名恢复旧链路。
- **接口和来源同时漂移。** 公共合同由唯一 owner 先固定，消费者随后迁移；区分 HEAD、固定候选和 live。按现行规范判断陈旧 fixture，保原业务断言；hash 常量须由独立 preimage 验证，不从生产结果抄期望。
- **helper 绿灯漏真实入口。** 本轮默认 Skill producer 缺 resolver、执行早于 durable provenance、恢复漏 stream locator 与 call/result payload 形态。真实 HTTP 工具 Job 被放到边界阶段，又暴露 stream/checkpoint carrier 配对错误；这类迁移先把一个完整生产入口放入少量核心，避免大组跑完才发现主链路失败，此项后续效果待验。核心覆盖真实装配→发送/封存→重启/恢复，保typed归属与内容hash。接口迁移同时迁spy/mock观察点；旧getter断言失败后手工lease未释放曾令teardown耗尽保护窗，测试用既有lease上下文或finally覆盖失败清理。真实lifespan的启动与关闭也须整体归同一明确owner。具名核心57已验证真实publication/generation同源、lifespan启停及Skill重启三项完整通过；随后整组正常收终态；其他差异继续同批收口，不能据此宣称完整边界通过。
- **测试重复等待同一前置，或结果与预算失真。** 本轮 fallback 的23例都因缺同一MCP catalog在准入前失败，整组153秒；先修公共producer/fixture，用少量原有用例穿过准入再展开。核心修通后完整fallback的31项通过，证实该组的窄效果；整批交付和模型提速仍待验。cProfile 外层0曾包着pytest失败，核原始终态与实际退出码。辅助等待预算与产品超时分别核因，保业务timeout、原断言和进程外保护；已知慢的跨进程组提前按文件/场景拆分，在保护窗内收完整终态，连续超时的截断日志不计绿。
- **人工补救与重复审查。** 同一固定候选已过检查按路径和运行依赖复用，只验新增差异及受影响入口。先核具名入口、失败栈、fixture、producer和owner，再归因；截断日志的进度点不能定位失败用例。反证成立立即撤单，主/审查错误与继承缺陷不计当前作者返工。
- **本地正文加密带来额外链路。** 用户已取消同目录 AES、两个 key 与 HMAC；保原 detail owner 单份精确正文、完整性、恢复/保留期和默认 history/API 脱敏，不迁移旧开发数据。联邦、SSH、认证密钥和 Provider opaque reasoning 仍归原 owner。
- **路径和索引误操作。** 每命令固定物理 cwd、私有 index、产物根；不 `/tmp`、不复制整树或自建 `.venv`。私有 index 不限制检索；审固定树并串行集成，共享 index 必须保持原字节。

具体技术失败按关键词查询[条件性细则](memory/2026/10/07/delivery-failure-rules.md)，当前结果与待完成项只在[实时台账](memory/2026/10/07/team-collaboration-state.md)维护。严重失败先纠正派单，再窄改规则；后续真实交付验证后才记有效，无效或重复规则删掉。历史事故不自动成为全队前置；窄闭包通过不勾整个 change，不结束原 goal。文档提交后的 HEAD 只记外部收据，不追逐自身坐标反复提交。
