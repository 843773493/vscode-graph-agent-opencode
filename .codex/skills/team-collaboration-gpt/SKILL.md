---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择 GPT 系列；技能不能切换正在运行的主模型。

执行前读取 [共同调度流程](memory/2026/10/06/team-collaboration-workflow.md)。队伍人数、派单、目录、提交和验收只在共同流程维护；模型配置只在本入口维护，不写回 `AGENTS.md`。继续实施或处理报错时，再读取 [实时状态与实测改进](memory/2026/10/06/team-collaboration-state.md)。

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

`task_name` 与 `message` 按共同流程分配，实际工具名以当次 schema 为准。模型 ID **不带 provider 前缀**，不能省略模型，也不能擅自添加 `newapi-local/`。

上述参数已通过只读探针，创建、回复和 `turn_context` 均确认成功，见 [模型探针记录](../../../out/tests/temp/2026/10/03/154301-gpt-model-probe/coordinator/artifacts/model-probe.json)。工具说明中的候选列表不能单独证明某个 ID 不可用；实际创建失败时明确报告原始错误并记录受影响任务，不自动换模型。

复用与中断恢复按共同流程核验；用户变更模型选择时直接修订本入口的唯一配置，运行状态更新共同台账。

## 当前效率试验

用户于 2026-10-05 指定一个 `gpt-6.1-sol` / `max` subagent 贯穿实现，暂停 Luna 实现者写代码；本轮按该授权覆盖上述默认配置，独立审查与主线程串行集成保留。实际创建及 `turn_context` 已确认 Sol 模型。暂不把单次试验改成永久默认，也不为满足常驻数量派发重复实现。

同日用户另指定主线程统一资源寻址术语及引用，按寻址 change 的 1.1/6.6 计划执行，与 Sol 在途业务接口错开；该机械正名单独计量，不混入主线程补救业务实现或模型效率比较。

以同一业务闭包记录首个完整候选所用时间、真实入口遗漏、必要测试首次通过、返工、依赖等待和主线程补救量；接手前的 Luna 投入及已完成前置单列，不把累计耗时差直接归因于模型。结果出来后据证据选择分工及模型，未验收前不宣布 Sol 更快。

已集成切片、当前候选与验收范围只维护在实时台账。本轮主补生产源码仍为零，但已接手 history、Gateway context、导航及领域 caller/fixture 迁移和崩溃验收，其改动、首轮失败和验收成本单列，不能记成主补救零。catalog/residency 稳定差异早审在完整包前发现并闭合生命周期缺口。继续让固定差异早审与调用方迁移、定向验证并行，复用未变审查；没有可比计时，不给速度倍率或永久默认结论。

## 按实际交付选择分工

主代理负责依赖协调、独立核验和串行集成，分工由实际结果决定。独立业务可由一个实现者贯穿；公共接口有多个消费者时，唯一提供者先固定完整合同，再按依赖推进消费者。用户建议是试验选项，不能推成全仓模板。粗切片队列曾漏列原任务的typed字段、residency回收和远端URI；按原task ID和入口对应证据，区分真实缺口、已实现待集成与陈旧规划，不把切片名称或总测试数当范围验收。

比较相近闭包的首次完整交付耗时、入口遗漏、必要测试首次通过、返工轮次、依赖等待和主线程补救量。没有可比证据时不宣布最优；原有前置与接手成本单列。已授权纵向链路的必要调用方、fixture 和恢复入口由同一实现者继续，例行参数迁移不反复停问。agent 名称与恢复摘要不能决定实际代码归属或撤销用户新调度。

已明确的例行 caller/fixture 迁移反复只有调查、没有可用代码增量时，先核实际改动和进程，再显式释放文件归属；可由主线程接手窄迁移，让同一实现者推进功能闭包，避免增加 writer。交接与补救成本如实计量，后续完整验收才证明此调整有效，不能因等待时间直接重启 agent 或宣称模型更快。

## 需要记住的严重失败

本入口保留决策规则；事故、候选与验证效果查[实时台账](memory/2026/10/06/team-collaboration-state.md)及其中原报告。未实测的调整不写成已解决。

- **交付残缺、漏真实入口。** 手筛hunk曾漏签名、删除和Web caller。用[交付校验脚本](scripts/verify_delivery.py)从固定树严格重建，核blob/hash及候选外源码；调用清单包括API/UI、CLI/进程启动、恢复、协议和间接kwargs消费者。同类必填签名一次迁完，核真实callee再跑代表入口；不存在的Saver属性曾阻断28项，旧签名曾让15项连续失败耗时299秒，不能加alias求绿。迁移规划也核原输入/输出和正式用例：曾漏图片读取、文本补丁删除/移动，新增能力延期不能废除已有能力。可行动增量先交，未知及后续范围分开，不等全量调查才回报。
- **合同与基线错配。** live、旧HEAD、固定候选混读曾误判接口。先辨Git对象ID与文件SHA256，后者不能交cat-file；一次误用曾引出无关环境调查。tree没有提交历史；tree到worktree比较须用该tree初始化的私有index或直接blob，错误index曾让主把未变fixture报成删除。唯一提供者先交完整合同和依赖，消费者待实现字段不能阻挡取得前置。闭集只在owner登记，消费者具名引用；同步清旧spec/验收要求，严格校验不能发现全部语义矛盾。early包可先审，必要caller闭合才集成。
- **身份、writer和跨载体约束漏项。** Thread/Session混用、内部通知伪造user Turn、取消后未收to_thread writer都曾过helper测试。核真实user/internal/child、HTTP/WS和恢复入口，只消费已校验admission；幂等比durable原请求。typed字段核owner/slot/revision CAS及未封存约束；descriptor/root policy、prefix/ToolSet与public/protected binding须联校，各自hash合法不足以通过。
- **未知结果和镜像脱节。** 确定拒绝漏回执、以资源存活证明动作完成、释放失败先丢句柄均曾污染owner。原业务owner在真实转换边界记录结果并同步镜像；未知副作用继续阻断，按精确身份/revision/终态核实后结清。回滚或释放失败不得发布verified/closed，普通列表不能清unknown。
- **生命周期被缓存遮蔽。** LRU驱逐仍被服务持有的catalog、关闭listener漏派生writer、卸载await窗口接受新claim都曾出错。唯一registry持有到真实释放；写前消费同一closing准入，先收finalizer/后台writer再关catalog，失败保重试句柄和generation fence。测试重启的新handle交原fixture管理，不能被临时function scope提前回收。
- **路径与索引误操作。** 相对编辑、默认cwd、裸Git和自建archive/.venv反复错根，私有index只隔离暂存。每命令固定绝对workdir/index/产物路径；复用指定完整WT，不/tmp、不复制整树。检索限已分配根，大型/不确定搜索带外部超时；无界rg扫/曾卡交付近九分钟。共享index从absolute git dir定位，变化先比mode/blob/stage；没有精确备份不称精确恢复，重复误操者收窄写权限。
- **验证不代表固定交付或实际发送。** live绿灯不归缺装配的固定包；保存每attempt raw/实际exit及源码指纹，管道和陈旧baseline标签不能代验收，Web build不代tsc。真实Chat转换曾把合法overlay变空：核实际Provider转换和HTTP正文；request hash含规范化wire与projector/version/profile、工具、附件及loss/redaction，不能用LangChain envelope代替。非Agent直连由真实构建入口显式标记用途；缺admission或native projection不能当作豁免，否则会放行漏绑定的Agent。字段排除只作用已识别的认证/传输/attempt载体，不能递归删schema或正文同名键；schema声明由真实wire入口路径及合法载体类型识别，不能仅凭任意嵌套properties或type=function/json_schema形状豁免，曾连续返工造成正文丢字段或凭据绕marker。分派wrapper不得绕过统一字段名/marker校验；曾逐wrapper手写字典循环只校child，六处直接凭据漏拒。合法声明保留，实际值须拒绝或显式脱敏；同时验合法schema和同形业务正文，域探针不代实际HTTP验收。seal幂等不承诺HTTP只发一次。fixture对照domain/callee与冻结preimage，保golden、原输入形态/断言、密文与AAD配对，不加默认/timeout求绿。测试和生产来自完整一致组合。
- **CSM模拟提交掩盖边界。** 旧测试伪commit绕过实际seal；迁后手动restore直接seal旧epoch即冲突。边界验收走真实view、checkpoint-versioned CSM与pending transition owner，首个成功新epoch seal才consume/applied；不能给fixture假epoch默认或跳source联合校验。单个合成fixture红灯也不能独自证明生产缺陷，须核实际caller。
- **目录正确仍越过职责。** Saver seal拼Provider请求、runtime做wire映射、函数内导入隐藏双向依赖，消费测试全绿仍未发现。固定差异早审核最近AGENTS职责/顶层依赖，纯转换交现有mapping/provider owner；不放宽AGENTS或保重导出双轨。读snapshot内恢复detail曾触发初始化写锁：先冻结plan/ref，快照外取不可变detail，保owner/revision/hash和准入。
- **崩溃测试漏持久化窗口。** importer只测完整临时文件，空/截断next仍阻恢复。覆盖创建、部分写含UTF-8、fsync、原子发布和另一进程重开，区分未发布与已发布损坏，不删审计求绿。正常退出不能证明强杀关闭安全。
- **恢复漂移与诊断过早。** 错schema tree、未核caller或仅看上层role曾误派缺陷。恢复先核基线/候选/报告根、真实类型、构造注入/MRO和完整链路；低层测试漏前置不等于生产漏前置，无caller/规范依据的假设列未验。反证成立立即撤单；旧生成数据、继承缺陷和主协调错误分列，不归错当前模型。
- **协议与重启权威遗漏。** 把Browser二进制帧当JSON、SQLite只存投影漏owner元数据、新客户端漏认证都曾失败。核精确路由的依赖和例外，再按真实协议/认证验收；overview 等复合读取需启动其实际辅助服务，不因通用代理印象改断言或加空数据默认。引用和authority由同owner事务保存，重开核完整对象/游标、失败零副作用，不以初始化或mock替完整产品入口。

审查按固定tree、路径和实际运行依赖复用，只审新增差异及受影响入口。依赖核查包含fixture的初始化、装配和恢复callee；直接owner blob相同不能证明整条运行链未变。依赖有差异时按真实输入判断影响，明确保留复用范围与未验项，不把静态差异自动变成整包重跑；DDL还须核初始化实际导入的SQL，不能只比入口函数。参数层规则变更须联验实际preimage生产者、封存与恢复入口：调用方包装层不等于wire参数层，不能用递归识别同名request键补豁免。主未写实现的窄增量由主独审，复杂公共事务用只读审查者，不为每包重复等整队。严重失败先纠正任务，再窄改技能；后续真实交付验证后才记有效，不叠同义规则，也不把继承缺陷和主错误算作当前模型返工。
