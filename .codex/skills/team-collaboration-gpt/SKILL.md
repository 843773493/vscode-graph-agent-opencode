---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择 GPT 系列；技能不能切换正在运行的主模型。

执行前读取 [共同调度流程](memory/2026/10/07/team-collaboration-workflow.md)。队伍人数、派单、目录、提交和验收只在共同流程维护；模型配置只在本入口维护，不写回 `AGENTS.md`。继续实施或处理报错时，再读取 [实时状态与实测改进](memory/2026/10/07/team-collaboration-state.md)。

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

合同固定后仍反复扩大调查、未交可行动增量时，核实际源码和会话；可显式中断后复用同一实现者，只派下一段具体生产动作，再按依赖继续完整闭包。Responses三路径已交付且相关84项通过，Saver三端口也已交可行动增量；后者参数提取窄审未接受，具体绑定/stream遗漏已退回同一作者继续修，不把“已交源码”当正确性证明。完整wire与总体效率仍待验。无增量时间或观察超时本身不构成重启理由。例行 caller/fixture 迁移可核验后释放归属由主接手，交接与补救成本如实计量，不宣称模型更快。

## 需要记住的严重失败

本入口保留决策规则；事故、候选与验证效果查[实时台账](memory/2026/10/07/team-collaboration-state.md)及其中原报告。未实测的调整不写成已解决。

冻结参数须核真实绑定和发送结果：框架会按 structured-output strategy 改写或省略 `tool_choice`，adapter 还会强制 `stream/include/store`。直接抄 `ModelRequest` 字段曾在固定增量早审中漏掉这些事实；先修提取再接封存，不等完整 HTTP 验收才查。 Auto策略fixture还须核框架模型名兜底，profile=False未必选工具策略；按实际策略提供HTTP输出，不将错配fixture归生产回归。

- **交付残缺、漏真实入口。** 手筛hunk曾漏签名、删除和Web caller。用[交付校验脚本](scripts/verify_delivery.py)从固定树严格重建，核blob/hash及候选外源码；调用清单包括API/UI、CLI/进程启动、恢复、协议和间接kwargs消费者。同类必填签名一次迁完，核真实callee再跑代表入口；不存在的Saver属性曾阻断28项，旧签名曾让15项连续失败耗时299秒，不能加alias求绿。完整模型E2E前先核真实路由状态码、异步回执和各窗口可见控件；主测试误设enqueue200、误点扩展窗口隐藏会话树，曾到末端才暴露并重复整场景。迁移规划也核原输入/输出和正式用例：曾漏图片读取、文本补丁删除/移动，新增能力延期不能废除已有能力。可行动增量先交，未知及后续范围分开，不等全量调查才回报。主独占验收候选固定后合并通知一次；状态消息不新增实现前置，作者不重复承担已分配的测试独审。连续通知曾引出重复读测试、下一生产增量延迟，收窄后的效果待实际交付。
- **合同与基线错配。** live、旧HEAD、固定候选混读曾误判接口。先辨Git对象ID与文件SHA256，后者不能交cat-file；一次误用曾引出无关环境调查。tree没有提交历史；tree到worktree比较须用该tree初始化的私有index或直接blob，错误index曾让主把未变fixture报成删除。唯一提供者先交完整合同和依赖，消费者待实现字段不能阻挡取得前置。闭集只在owner登记，消费者具名引用；同步清旧spec/验收要求，严格校验不能发现全部语义矛盾。early包可先审，必要caller闭合才集成。
- **身份、writer和跨载体约束漏项。** Thread/Session混用、内部通知伪造user Turn、取消后未收to_thread writer都曾过helper测试。核真实user/internal/child、HTTP/WS和恢复入口，只消费已校验admission；prepare与封存读取须消费同一规范checkpoint namespace，曾漏传namespace而查错owner，缺失记录必须明确报source-mismatch并保原因；幂等比durable原请求。首次final与checkpoint重复提交、重放和fork须联验同一model-call身份；codec从消息自身取权威值，显式caller或持久字段冲突必须拒绝，不能放宽typed provenance guard。typed字段核owner/slot/revision CAS及未封存约束；descriptor/root policy、prefix/ToolSet与public/protected binding须联校，各自hash合法不足以通过。
- **未知结果和镜像脱节。** 确定拒绝漏回执、以资源存活证明动作完成、释放失败先丢句柄均曾污染owner。原业务owner在真实转换边界记录结果并同步镜像；未知副作用继续阻断，按精确身份/revision/终态核实后结清。回滚或释放失败不得发布verified/closed，普通列表不能清unknown。传输授权只接受精确入口的专用no-effect拒绝和同身份、工作区、verified、非倒退revision快照；一般409/超时不能当确定拒绝。
- **生命周期被缓存遮蔽。** LRU驱逐仍被服务持有的catalog、关闭listener漏派生writer、卸载await窗口接受新claim都曾出错。唯一registry持有到真实释放；写前消费同一closing准入，先收finalizer/后台writer再关catalog，失败保重试句柄和generation fence。backend unload依赖aux时先完成backend关闭再停aux；整组同时TERM曾令revision同步失败、owner未知和Application shutdown failed，safe restart保aux的成功不能代此验收。核精确owned进程组的活成员，根PID退出不能证明子进程已退；root-only检查曾让真实关闭绿仍漏子进程。测试清理也从首个进程起覆盖，按原组回执重扫；child尚未登记时依赖child PID会漏收，须覆盖根已退/子回执未写的故障窗口。超时强杀只证明回收，不能证明drain/unload完成；原handle保失败，已退出后重试也不能洗掉事实，不能增大预算求绿。测试重启的新handle交原fixture管理，不能被临时function scope提前回收。
- **路径与索引误操作。** 相对编辑、默认cwd、裸Git和自建archive/.venv反复错根，私有index只隔离暂存。每命令固定绝对workdir/index/产物路径；并行校验各用独立index，write-tree会更新缓存并取锁，曾共用私有index令验收启动失败；复用指定完整WT，不/tmp、不复制整树。长产物路径曾使Chromium的Unix socket启动失败；Linux可用持有目录fd的短`/proc/<owner_pid>/fd/<fd>`引用，先探针核真实目标，全部子进程退出后才关fd，物理产物仍留正式runtime/tmp，不新增工作区副本。检索限已分配根，大型/不确定搜索带外部超时；无界rg扫/曾卡交付近九分钟。共享index从absolute git dir定位，变化先比mode/blob/stage；没有精确备份不称精确恢复，重复误操者收窄写权限。
- **验证不代表固定交付或实际发送。** live绿灯不归缺装配的固定包；保存每attempt raw/实际exit及源码指纹；tree参数从当次manifest读取，手填错ID曾令测试未启动。管道和陈旧baseline标签不能代验收，Web build不代tsc。Web失败先收全部实际窗口/iframe的console、失败请求与错误控件aria/title；只截主窗、只读图标文本曾延迟定位ERR_NETWORK_CHANGED。先用不调用模型的实际加载探针缩范围，不把integration的route.fulfill搬入E2E，也不凭网络失败改生产。参数提取也先对照原流式/非流式入口与省略参数：共享helper曾强制非流式发送stream=True并重新带入None采样值，两个真实入口早验证实回归；同一作者修正后4项及相关33项通过，完整wire仍待验。真实Chat转换曾把合法overlay变空：核实际Provider转换和HTTP正文；request hash含规范化wire与projector/version/profile、工具、附件及loss/redaction，不能用LangChain envelope代替。非Agent直连由真实构建入口显式标记用途；缺admission或native projection不能当作豁免，否则会放行漏绑定的Agent。字段排除只作用已识别的认证/传输/attempt载体，不能递归删schema或正文同名键；schema声明由真实wire入口路径及合法载体类型识别，不能仅凭任意嵌套properties或type=function/json_schema形状豁免，曾连续返工造成正文丢字段或凭据绕marker。分派wrapper不得绕过统一字段名/marker校验；曾逐wrapper手写字典循环只校child，六处直接凭据漏拒；匹配须覆盖现有认证header别名及大小写、连字符形式。合法声明保留，实际值须拒绝或显式脱敏；同时验合法schema和同形业务正文，域探针不代实际HTTP验收。seal幂等不承诺HTTP只发一次。fixture对照domain/callee、实际codec必填字段及冻结preimage，保golden、原输入形态/断言、密文与AAD配对，不加默认/timeout求绿。Provider漂移输入须成组改变有效profile、声明与参数；只改旧字符串会提前落到source-mismatch。同epoch换profile须真实rebuild边界，主fixture曾违反该限制导致两红；首次封存、同epoch拒绝与合法重建分开验证，不放宽guard求绿。applied断言随唯一owner迁移，不能恢复已退出真实链路的第二writer。测试和生产来自完整一致组合。参数化真实服务测试还须隔离每个参数的持久active配置与控制面状态；只改JSONC会复用旧endpoint，曾让Chat访问已停的Responses替身。拒绝零写入要比完整checkpoint/config/metadata和原始持久字节，不能仅比ID。
- **诊断跟踪改变时序。** 全场景ptrace曾把真实PTY停止从2.96秒放大到10秒请求超时；超时先在相同真实入口对照有/无跟踪，原失败保留，移除跟踪后再验完整场景。进程外timeout/内存保护继续保留，不增加生产timeout或删除业务断言求绿。跟踪限定必要进程与阶段，不能将被放大的时序直接判成生产缺陷。
- **CSM模拟提交掩盖边界。** 旧测试伪commit绕过实际seal；迁后手动restore直接seal旧epoch即冲突。边界验收走真实view、checkpoint-versioned CSM与pending transition owner，首个成功新epoch seal才consume/applied；不能给fixture假epoch默认或跳source联合校验。共享view入口须逐boundary核副作用：曾把只读history_replay映射成rewind，倒改tracking并登记伪pending；历史projection不得恢复CSM或触发执行epoch；泛化active-view条款曾与history只读语义冲突，公共spec/design/caller一起修订，不能只改实现或追加任务；只查pending行不变会漏active branch与pending归属交叉；旧Turn/execution诊断probe不能证明合法继续失败，须由accept_turn/replay_as_new_turn取得真实新Turn后联验seal/reopen及pending生命周期。目标checkpoint仍可见的snapshot/untracked正文须从旧sealed selection/detail携带；只按tracked batch重建曾在早审暴露正文丢失；cutoff前携带/后消失已由真实边界验证，效果限该范围。来源fixture区分完整SKILL.md与解析正文，真实冻结入口缺frontmatter曾挡住六场景，先验证共同fixture再展开。失败回滚沿真实seal写面一次列齐正文/detail（含孤立行）、投影、selection、sealed commit/cursor、完整plan/ToolSet绑定及dispatch；关联查询或只查plan两列曾连续漏半提交断言。合法unsealed plan/compact registry比较预存状态，rollback后独立failure audit具名排除，新正文/detail仍在内存；提前登记detail再标tombstone不是回滚；正文/key先prepare再同seal登记发布。拆分后曾因登记要求未发布父目录存在而阻首次seal，须验fresh owner与publish/commit失败，继承tombstone期待同步迁完整无残留断言。compact草稿provenance仍按已定合同允许，保既有行不变并具名核新行无绑定，不把全registry不变误作原子性。fork须联核selection contribution与ref provenance的映射，并从pending/consumed/continued真实状态复制后seal/reopen；普通fork绿灯曾漏领域引用冲突和view membership，修后本次三phase通过，静态前驱/source身份闭包另验。严格领域ref变更须验比较、持久化恢复与retry；omitted仅保身份时不能重造缺正文的public ToolSet，完整wire ref校验仍保留。单个合成fixture红灯也不能独自证明生产缺陷，须核实际caller。
- **目录正确仍越过职责。** Saver seal拼Provider请求、runtime做wire映射、函数内导入隐藏双向依赖，消费测试全绿仍未发现。固定差异早审核最近AGENTS职责/顶层依赖，纯转换交现有mapping/provider owner；不放宽AGENTS或保重导出双轨。读snapshot内恢复detail曾触发初始化写锁：先冻结plan/ref，快照外取不可变detail，保owner/revision/hash和准入。
- **崩溃测试漏持久化窗口。** importer只测完整临时文件，空/截断next仍阻恢复。覆盖创建、部分写含UTF-8、fsync、原子发布和另一进程重开，区分未发布与已发布损坏，不删审计求绿。正常退出不能证明强杀关闭安全。
- **恢复漂移与诊断过早。** 错schema tree、未核caller或仅看上层role曾误派缺陷。恢复先核基线/候选/报告根、真实类型、构造注入/MRO和完整链路；低层测试漏前置不等于生产漏前置，无caller/规范依据的假设列未验。反证成立立即撤单；旧生成数据、继承缺陷和主协调错误分列，不归错当前模型。
- **协议与重启权威遗漏。** 把Browser二进制帧当JSON、SQLite只存投影漏owner元数据、新客户端漏认证都曾失败。核精确路由的依赖和例外，再按真实协议/认证验收；overview 等复合读取需启动其实际辅助服务，不因通用代理印象改断言或加空数据默认。辅助进程沿生产launch与最近AGENTS选择运行时，曾用Bun启动Node PTY服务导致立即SIGHUP，不能据此误派生产修复。退出码须核运行库信号约定与实际lifespan/unload收尾；Uvicorn正常返回后会重抛SIGTERM，不能一律要求exit0，也不能仅凭-15证明成功。引用和authority由同owner事务保存，重开核完整对象/游标、失败零副作用，不以初始化或mock替完整产品入口。

恢复先读当前坐标及相关收据，历史台账按任务查询，大型JSON只取本次相关键。未变规划与候选核指纹后复用已验收据，不能每次恢复全文重读或重新展开完整检查。当前队列及时移除已完成动作，历史attempt不标为进行中；曾残留已验Web场景的“第四轮运行中”，与最新收据冲突。台账只记录范围、结果和调度的实际变化；文档提交后的HEAD由外部收据记录，不为追逐文档自身坐标重复提交。

审查按固定tree、路径和实际运行依赖复用，只审新增差异及受影响入口。先确认旧报告确实审过这些实现路径；前置清单或fixture差异审查不能充当生产实现接受。消费者窄审附原owner已定合同的精确定位与本次未验边界；漏给合同曾令独立HTTP全等断言被误当待定，不能改弱断言替代生产闭合。曾因重复调查已完成的问题或反复追加报告收尾检索而迟迟不交结论；先交已有发现与未验边界，精确行号打磨不阻挡可行动回报。反复扩查可显式中断，同会话只派报告交付；一次报告专派已实际交付，原未证边界保留，不能据此宣称总体提速。关联缺陷单独登记，主线程决定后续归属。依赖核查包含fixture的初始化、装配和恢复callee；直接owner blob相同不能证明整条运行链未变。依赖有差异时按真实输入判断影响，明确保留复用范围与未验项，不把静态差异自动变成整包重跑；DDL还须核初始化实际导入的SQL，不能只比入口函数。阶段报告归档核本轮运行身份和来源更新时间，未触达阶段不复制上轮产物充数。参数层规则变更须联验实际preimage生产者、封存与恢复入口：调用方包装层不等于wire参数层，不能用递归识别同名request键补豁免。主未写实现的窄增量由主独审，复杂公共事务用只读审查者，不为每包重复等整队。严重失败先纠正任务，再窄改技能；后续真实交付验证后才记有效，不叠同义规则，也不把继承缺陷和主错误算作当前模型返工。
