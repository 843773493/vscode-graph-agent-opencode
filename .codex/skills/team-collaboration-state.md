# 团队协作实时状态与实测改进

供两个团队入口共同引用。恢复 goal、派单、集成或处理报错时读取；稳定操作规则维护在 [共同流程](team-collaboration-workflow.md)，本文件只记录当前事实与已观察的调整效果。主代理是唯一写入者。


更新时间：2026-10-04 09:52（北京时间）。goal 持续实施；使用 GPT 全队，常驻实现者与两名独立审查者按依赖推进，U08 受 429 中断并保留证据，不密集恢复。先核对本节事实再恢复，历史服务状态只表示上次验证结果。

| 项目 | 已核验状态与下一步 |
|---|---|
| 当前范围 | 用户已启动交接任务与关联 OpenSpec 实施、缺陷修复、冗余清理及技能实测改进 goal；按七个关联 change 的依赖推进；UUID自然分配及路径预算已集成，真实回拨 U07 已集成，完整关联回归 684 项及定向修正/自然分配 1+3 项通过、A15b 独审通过；E/T/U 执行与历史闭包、W01/W02 多工作区路由及 V01 Session VRN 链继续实施，不自动扩展到新需求 |
| 版本与模型 | 两个版本入口维护唯一模型配置；GPT 精确 ID 的创建、回复及实际模型记录已通过 [只读探针](../../out/tests/temp/2026/10/03/154301-gpt-model-probe/coordinator/artifacts/model-probe.json) |
| Git 基线 | 当前主树 HEAD `5383baf3`；业务集成至 U07 `b75dbd90`，UUID 验收与技能文档 `c894e7be`、证据更新 `5a958b20` 已提交。T01 `0a73ae37`、G01/G02 `72e755eb`、S01 `ad093e0c`、U01/U02 `c16b2c17`、G03 `7596a841`、S02 `122e7939`、验收文档 `a625db31`、T04 `fd6292da`、UUID关联测试记录 `0a7eda26`、配置来源 `f42b6f77`、S03/S04规范 `04a5e30d`、UUID自然分配 `e27260ca`、配置公共 VRN 字段 `69d77eea`、S05 规划 `710ffca4`、C03 验收 `d1aa533d`、U06 路径预算 `00edda52` 已串行集成，防线与祖先链通过。B01/B02/J01已集成28359675，R19独审通过，74+1pass/imports0；S06-v2四份规划与技能已集成5383baf3；新鲜独立索引检查主树干净，保留共享暂存 |
| 迁移与历史清理 | 数据盘物理根及旧入口软链接已核验，旧副本均删除，系统盘可用空间增加约 238GB；另删除 14 份可重建副本、分类整理 67 项、校验 23092 个保留文件。详见目录审查及其清单 |
| 业务数据清理 | 默认工作区与 Drive 的旧 .boxteam 分别获用户授权清空，普通文件保留；用户随后统一授权删除源码开发生成的新旧中间数据；该授权不包括普通源码与未集成独有改动。仓库根的旧终端测试目录及空 .boxteam 已另获授权删除，创建者未确认 |
| 开发服务 | 上次核验默认与 Drive 均为 ready，前端 8027 三条初始化 API 为 200 且 request_id 一致；管理 dev:status/dev:stop/dev 使用下述原环境，恢复时重新检查 |
| 验证与证据 | T01 activity 1、history 20 项通过；G01/G02主树受保护10 passed（3.54s），旧顺序变异有效。T02 generation 最终4 passed（41.69s），R04发现消息展示断言移除，当前拒绝集成、复核canonical展示合同；T03锁定internal root未推进执行归属，T04真实writer修复已集成并主树2passed/1.76s；新版runtime_notice还须E01独立execution binding。U01/U02独立A03通过并集成c16b2c17，主树117 passed/24.20s、profile负向1passed/0.22s；UUIDv7十一份完整关联测试主树685passed/129.82s，保护命令与结果已存JSON；完整 users.py Ruff 有18条既有诊断 |
| 明确裁定 | `assembly_ref=ResourceIdentity`；统一寻址拒绝码 `unsupported_view`；Gateway ID `current` 创建拒绝；config layer只表示逻辑来源、读侧sqlite换runtime_override、优先级不变、快照独立。delegate 复用 Session SQLite collaboration ledger 同事务发布，不另建 Team JSON intent；internal admission 无 Turn/accepted ingress，输出保留原 semantic、显式 execution link。证据 `coordinator/artifacts/owner-decisions.json` |

活动任务统一根：`out/tests/temp/2026/10/04/024121-team-execution/`；既有九个 agent 的创建参数与实际 turn_context 均已核验为 `gpt-6-luna / max`，创建时均 `fork_turns:none`；模型证据见 `coordinator/artifacts/model-context-latest.json` 与 `u05-model-context.json`。下表路径均相对物理根，派单始终给绝对路径。主代理独占技能与完成台账；spec_writer 在独立 worktree 修订 S03/S04 已裁定规范，不勾实施任务。U05 首轮 429 后已按同模型恢复。

| 编号 / agent | 状态与范围 | 工作目录 / 产物 | 验证与下一步 |
|---|---|---|---|
| U01–U03/J01 / uuidv7_implementer | U01/U02已集成；U03固定source/tests补丁9f5bba3c/8c34c823，中央34/34hash匹配，R17在途；J01本轮429，主代理接管fixture并集成28359675 | out/worktrees/2026/10/04/024121-team-execution/internal_display_projection / 同名任务产物根 | 基线7596a841加完整E01依赖；reader真实import/signature通过，保union exports。分页7pass、Web37项/132断言、tsc/build绿；旧generated已真实生成闭合；integration20例阻于E01 _rollout_id签名。7/37通过记录为工具输出转录、退出码推断，已明确标注。J01九测试去fixture注入后AST9/9同基线；必填guard与10份fixture中央同步，主树74pass（含Bus5）；B02独立集成1pass，imports0；U03b从S06-v2实施共享坐标/独立真实SQLite回归；两read projection、primitives、DTO/proto/Web独占，E01写侧不覆盖；原U03冻结不改 |
| R01–R04 / behavior_reviewer | T01/G01/G02审查已落盘；R03发现U01恢复durability blocker；R13审E01固定snapshot2已完成；R14审W01最终固定88文件已交报告，R15审history ports | 主仓库 / 任务根的 behavior_reviewer | R04b已厘清完整旧metadata过时但安全展示丢失真实，U03实施；R05找到S02逐记录VRN/layer歧义，修正已复核，R06通过T04；R07发现B01删除失败后晚准入P1，交E01/W01；R08通过C01；R09b通过S03最终合同；R11通过C03、R12通过U06；R13定位真实user acceptance、terminal convergence及admission context接线缺口，E01收口；R14纯身份路由无新缺陷，7startup失败W02在途 |
| A01/A02 / architecture_reviewer | 依赖报告与U01架构复审已落盘，无新增架构blocker | 主仓库 / 任务根的 architecture_reviewer | A03/A04已完成并支持UUID/G03验收；A05残留审查完成；A06拒第二Team JSON权威；A07发现UUID真实allocation破D2；A09b通过S04最终合同，A10确认SQLite主路径504/505真实边界；A11核internal display-only view成员和Provider全局pending泄漏；A13确认真实ThreadRuntime/sweep未接线且Trace旧侧车已无，A14交Session VRN纵向计划，A15b通过U07，A16复审W01 |
| G01/G02/C01 / gateway_user_implementer | G01已集成72e755eb；C01/C02已集成f42b6f77，C02在429后由主代理接管；C03已集成69d77eea/R11通过；S05最终规划复审通过；V01固定交付已交，A22独审在途 | C01 worktree已核27差异等集成HEAD且无进程后回收；V01：out/worktrees/2026/10/04/024121-team-execution/session_vrn_owner / 产物根gateway_user_implementer；C03旧证据保留 | R08无阻塞；C03主树46+10+73+2项验证、生成/tsc/build通过；V01从230e9dc2独立worktree统一ResourceIdentity与结构化Session引用/cursor/scope及API/Agent/Web；W01 backend_workspace_id schema及Web request target已中央精确同步/真gate通过；与W01仅container构造片段共享，context client认证已补窄supplier；V01定向56unit+11HTTP+1真实Web集成通过，1024预算显式拒绝、1600真实16页收敛；unit-web初次1227pass/2fail为异步hash取消竞态，修复后1229pass/0fail保两份原始日志；54文件b3d4c440+defc0ea1中央严格重建54/54hash，真实模型E2E缺有效配置 |
| T01–T05/B02 / integration_fixture_implementer | T04已集成；T05固定38be8c13中央4/4hash匹配，A17通过结构审查；R16发现metadata覆盖归属与首次输入跨事务，交T05/E01分别闭合；B02最终交付并集成28359675 | out/worktrees/2026/10/04/024121-team-execution/integration_fixture_implementer / 任务根的 integration_fixture_implementer | T05 Ruff0、2pass/2fail，红灯在E01 acceptance root NULL。B02已迁integration，真实catalog删除失败1pass/Ruff0，两个单gate完整archive变异各exit1且精确命中各入口；中央1pass，R19通过，集成28359675。T05后续独立增量明确拒绝user_turn下新internal HumanMessage；T05归属冲突a93893e0增量1pass且原始日志保存，首次notice原子输入接口归E01。中央同步E01八文件和hashfix后实际7pass/2fail到read snapshot身份缺失，E02在T树修；作者现转M01新树，不重复T测试。ThreadRuntime下一切片计划已交 |
| S01/S02 / spec_writer | S01集成ad093e0c；S02集成122e7939；S03/S04经主代理修订、R09b/A09b通过并集成04a5e30d；writer暂停写入，worktree已保独有source overlay后回收 | 原out/worktrees/2026/10/04/024121-team-execution/spec_writer / 任务根的 spec_writer | S01两个strict通过；3.1/3.2错误历史实现勾选已撤销，规范决定另登记，不混完成含义 |
| W01/W02 / workspace_owner_implementer | 已授权registry→服务图→Session导航及CRUD→Gateway/bootstrap→Web双workspace闭包 | out/worktrees/2026/10/04/024121-team-execution/workspace_owner_implementer / 任务根的 workspace_owner_implementer | 基线ad093e0c，codex/workspace-session-mount；88文件routing/API/Web补丁80aa6489中央88/88hash；真实双graph160并发、Gateway475、Python74/Web130/reload5通过。W02 e0976d固定13文件中央13/13hash，explicit managed/attached与原锁复现1pass；六caller首跑3pass/3fail，aeefd07a已冻结、catalog fixture通过；owned-process/stub/registry八文件42de6281严格重建8/8，2+7+1+1定向通过，A21独审发现P1待owner修复；W03真实launcher supervisor handoff 1pass/30.18s，原Browser PID/page合同保持，单文件25f8f935中央strict1/1hash；A21发现pending失败清理杀adopted backend，owner小增量修，physical fork仍需completed source fixture。Session guard59f6b及import-order c0d7b已交；context认证401归V01。不得将局部绿灯算作完整集成通过 |
| E01 / thread_owner_implementer | 已授权8.3-E执行完整闭包与Job/pending/message真实Thread入口 | out/worktrees/2026/10/04/024121-team-execution/thread_owner_implementer / 任务根的 thread_owner_implementer | 基线ad093e0c，codex/thread-execution-owner；domain39passed、sibling Thread acceptance7passed，仍须main-child/Job→Step/model-call/stream/Web闭合；采用typed owner ContextVar，fork/index reader依赖已补，admission2项通过；锁alias回归通过；新owner八文件abef6f88及六prerequisites严格重建成tree f2a6f744，manifest8/8hash，中央同步T05保union。c25ed561哈希增量中央strict4/4匹配并sync；actual7pass/2fail已越过hash，失败到prefix restore把thr当ses，E02派给V作者在T树修4文件read片段；R20窄8静态无另项独立缺陷，未把依赖红灯算绿；assembly thread_id writer/reader、首次notice同事务及JobStep完整入口继续闭合；ports b282fa重建5/6hash匹配，中央恢复遗漏codec两行后6/6一致，四wrapper已同步U03/T05。R15真实SQLite探针发现Turn visible谓词漏canonical，window随后TypeError，E01单独修；A11要求复用view display_only显式成员，v2按canonical rows有界续读 |

| E02 / gateway_user_implementer | V01冻结后转 checkpoint读取身份闭包 | out/worktrees/2026/10/04/024121-team-execution/integration_fixture_implementer / 产物根gateway_user_implementer | 当前T/U/E完整union，固定4文件preimage已留coordinator/snapshots/e02-read-owner-preimage；只改langgraph adapter/open_snapshot/index/service wrapper，E01 assembly与U03b catalog/primitives保持独占；真实9gate及child/main restore待验证 |
| M01 / integration_fixture_implementer | A20报告已交，新增窄非Job Session写入准入实施 | out/worktrees/2026/10/04/024121-team-execution/session_mutation_admission / 任务根session_mutation_admission | 基线5383baf3，分支codex/session-mutation-admission；update、idle compaction、用户Goal facade复用topology→单Session→catalog准入，runtime派发在gate外；实际deleting拒绝/排空并发回归待验；T05旧worktree保留独有source，暂不重跑 |

| U05 / canonical_allocation_implementer | 修 UUID 真实 Session/Thread canonical allocation，规范由 S04 独占 | 原out/worktrees/2026/10/04/024121-team-execution/uuid_live_allocation已保source overlay/hash后回收 / 同名任务产物根保留 | 基线7653dd92；18文件已集成e27260ca、主树684pass/Ruff0/双独审；主树浅根255key通过不能关闭隔离深根516-byte真fail。U06已集成00edda52、主树433pass/R12通过，Linux VFS512预算与零副作用证据确认，Windows/macOS未测；U07真实allocator回拨/跨午夜与fixture毫秒校验补丁9743e079中央2/2hash、Ruff0、主树9pass；完整11模块684pass，A15b指出并关闭同日毫秒/实际catalog无写覆盖；中央定向1+3pass，已提交b75dbd90；U08受429未重试，剩余验收中央完成 |

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
| S02恢复后又读取主树，误称已确认规范被并发回滚；八文件改动在分配worktree完整 | 中断并直接比较worktree文件与主线git show，重发字面cwd/分支/索引；主代理统一提供准确差异证据 | 无源码/spec越界写入；恢复后原worktree严格校验并准确交付S02，本次纠正有效，恢复cwd规则仍须观察 |
| T02四项通过但移除了messages公开展示覆盖；T03同轮新数据history暴露归属写入错误 | 拒绝以当前丢字段行为为设计，独立查codec/factory/Web及canonical合同；T03生产文件从E01精确释放给单一实现者 | R04b确认display policy事实在round-trip丢失；T03锁定internal root未推进Turn，writer修复已派T04 |
| W01将双graph探针写入未分配的out/tests/temp/w01-dual-mount；C01/E01已授权跨层闭包仍再次请求范围确认 | 重发字面产物根并要求清理越界探针；派单明确共享文件片段与生成器负责人，阻塞接口先交字段合同并继续独立部分 | W01曾纠正后又用worktree/out下短名；主代理保留失败启动证据并删可复现副本，重发task根；C01主树静态/构建与127测试均通过 |

| S03/E01把产物写进worktree/out，却回报主树同名路径；主树S03仍旧583b而新2896在分支 | 交付统一使用字面绝对产物根，消费前核实际hash；主代理将固定版本移至分配根 | U03依赖正确；S03后续artifact再次漏最终改动，主代理逐文件定位、导出完整patch并独审集成；作者自报主树越界未被当时文件证据支持 |
| E01声称21文件依赖可独立使用；两次T05验证先报fork identity签名冲突、补后又报index reader签名缺失 | 稳定接口须交完整生产callee闭包，先全仓核参数调用与SQL写入；消费者工作树通过后再派writer实现 | signature补齐后admission2项通过，真实append自锁已由E01修复；完整snapshot待交后恢复T05，不以writer归属未填的红灯反堵依赖 |
| UUID工厂与关联685测试绿，但真实Session/child allocation调用显式timestamp并破D2 | 按真实生产入口审契约，保同ms强单调，统一用自然分配ID内嵌时间派生桶 | A07独立确认；U05/S04 已集成，主树684项通过；真实回拨与深路径另开 U06/U07，未整体验收 |
| U05交付报告给未跟踪新测试手填hash，R10实核不符；W01 API绿但固定patch缺既定Web/DTO闭包 | 清单覆盖未跟踪源码并实算hash；固定patch后新slice另导；验收按原完整入口核，不以proxy转写替代Web identity | U05纠正后18/18hash匹配且3回归重跑通过；W01承认Web未迁并继续用户链路，尚未验收 |

| S05 新增合同仍留旧 staging/view 文字，引用不存在的 default_include；v2 cursor 未冻结过滤条件 | 只修改对应 owner 合同，沿 initial/anchor 既有配置；冻结 include/query/window，拒绝同 cursor 换条件；提供最新版四文件固定 patch | C04 最终五文件复审通过，S05已提交710ffca4；U03 实现验证在途 |
| U07同日毫秒用例原本跨日且检查了错误SQLite文件 | 改同日1ms差并比较真实catalog及状态字节，保留真实allocator时钟验收 | A15b复核通过；修订单例1pass，完整关联684pass；Linux之外时钟shim未实测 |

| W01 路由隔离 patch 引入 API→具体组装根依赖，并混入 main-thread 物理布局 | API 消费窄结构协议；存储改动拆开交由线程 owner，固定旧 patch 保留，新交付另导 | W01 已移除具体依赖并恢复两处非必要布局，重新冻结与独审待完成 |

| E01 snapshot2 hash/reverse-check通过，但混合绝对新文件patch路径且新mixin错导/漏组装 | 固定交付全为repo-relative，完整生产import与接口gate必须真跑；中央从固定index重建目标并串行同步，保U03独有exports | 作者已改repo-relative；中央消除ImportError后9项gate通过；后续ports四wrapper实际import/signature通过，不把hash一致当可运行证明 |

| 中央同步E01整文件覆盖已合并U03 codec两行；ports作者清单有新合同但补丁遗漏，实重建仅5/6hash匹配 | 固定基线应用补丁重建后对目标hash；共享文件保其它owner合同片段，不能只核作者工作树 | 中央恢复精确两行后6/6hash一致；U03四wrapper实际import/signature与F/I Ruff通过，T05继续writer；业务闭包尚未验收 |

| 主代理恢复W01时只写“主root/task/artifacts”，作者再次询问路径；C04转任务报告误写另一reviewer根 | 主代理每次派单补完整字面路径，报告提交前比对最新派单；旧位置搬回指定根，不创建第二报告 | W01已收到字面根；C04最终报告中央迁回config_vrn_contract/artifacts，后续派单待继续验证 |

| W02健康探测1s超时把显式已有backend当managed，二次spawn撞SQLite lock；失败发生在清理try之外 | 测试拓扑显式attached/managed，移除probe决定owner及sentinel，整个启动scope统一清理 | 已确认相同URL 1s失败/3s成功与实际lock PID因果；timeout-only修复未接受，完整W02在途 |
| U03 build绿但tsc红，旧generated无session/thread且仍outcome，作者计划删fixture身份字段 | 按实际DTO/proto保身份与Status唯一合同，独立生成验证，中央统一合并生成根 | 已重发唯一合同与生成责任；当前仅7项Python与build通过，tsc闭合待验 |
| R15固定SQL探针中Turn页接受display_only root，window仅查canonical随后TypeError | 存储owner统一canonical可见谓词并显式诊断空bounds；保留可重放探针，不由caller兜底 | E01 71f1ce4c中央2/2hash匹配并同步U03/T05，真实mixin probe通过；独立修复复审待排。原异常尚不能证明正常writer会生成坏membership |

| E01再次把R15/codec/probe落到worktree/out，却回报主树同名路径；消费前实际FileNotFound | 主代理拒收并要求搬回唯一字面根、实算hash、删除错误副本；保留原证据不另建规则副本 | 已搬回并实核71f1ce4c，2/2目标重建匹配、consumer真probe通过；路径遵守仍未稳定，不能称已解决 |
| W01固定container引用SessionService guard，但88文件patch不带方法；live测试不暴露遗漏 | 从固定graph调用边查完整callee；独立增量交59f6b并主代理串行同步真实method gate | E01 active/deleting方法gate通过，Job入口与失败后晚准入真实回归仍待闭合 |
| R16发现user_turn新internal消息由metadata改成无Turn，且internal admission与first notice各自commit | 归属冲突由writer明确拒绝；首次输入与admission交同一SQLite owner事务，新增独立补丁，不伪装跨事务原子 | 已分配T05/E01；B02独立catalog删除失败回归1pass、完整archive去两guard后预期红，原始退出码0/1已核；上述writer原子合同仍待验证 |
| U03通过记录未在执行时落盘，工具session过期后仅能转录；V01又拟手写临时类型迁就旧generated | 保存实际进程日志/退出码；转录显式标记推断。消费者独立树可真实生成用于验证，中央最终再生成，不引入第二DTO | U03已如实标记转录，未为补日志重跑；V01已重申真实gen→tsc→build授权，待验证 |
| A18核W02自动scope仍调用按port全杀，registry构造异常发生在lifespan接管前 | 按真实handle/process group回收，构造失败仍由构造owner关闭局部runtime；端口占用不代表可杀 | W02 owned-process与registry构造失败回归各1pass，固定42de6281中央严格git apply重建8/8hash匹配、A21已发现pending adopted handles清理P1，owner修复；共享stub归并及2+7+1+1focused通过，W03真实handoff已派 |

| B02同时删除两个gate的变异只证明外层拒绝，跨真实模块回归放unit；Job guard可选构造留下无效服务 | 测试归integration，增加prepared continuation真实callee断言，两入口分别单点变异；依赖必填且全量fixture显式注入 | 两独立变异各在目标断言exit1；中央Job/Bus74+集成1pass，AST9/9证实未删旧断言；R19独审通过并集成28359675 |
| R18用陈旧status索引当作已声明base-relative patch的preimage，误判规划需迁就旧索引；view排序合同也缺owner/同序约束 | 候选明确base与postimage index两种角色，不在旧status index上应用；补同view唯一稳定与Turn严格同序，缺损明确拒绝 | S06-v2 strict通过，R19确认合同闭合可作实施基线；display-only、派生顺序/同序校验/revision与真实SQL行为仍未验收 |
| A19发现非Job Session mutation绕过持久deleting；W02旧Browser用例普通stop/start却要求PID保留 | Session短写复用既有topology/session gate与catalog，不另起通用Job状态；Browser测试迁真实supervisor handoff且保全部业务断言 | A20报告已交并派M01三个入口；W03真实handoff1pass，A21的candidate失败ownership仍需独立修复 |

| E01反复全量回放仍漏rollout_index_reads，中央七项严格重建后又暴露新鲜admission hash错误 | 单一候选tree与manifest列全依赖，先给可复现callee失败；区分acceptance内容hash和admission请求preimage，不以开发数据清理避开缺陷 | 七项严格apply成功、8/8hash；原6pass/2fail日志保；hashfix后7pass/2fail到读取身份，转E02完整readclosure；作者已清可复现重建副本，业务闭包仍待验 |
| V01引入异步WebCrypto后隐藏检查面仍发取消请求，unit-web1227pass/2fail | 真实请求前核cancel，测试等实际启动；冻结交付保原失败与后续通过两份证据 | 已交作者修复；真实Web/Gateway/Chromium1pass，完整矩阵复验1229pass/0fail，初始两失败日志保留 |

| A21发现W02构造失败无条件close会杀pending candidate已adopt旧backend | 失败清理复用既有adopt/new所有权，detach旧handle、关闭新spawn，不用全registry保留造成泄漏 | W03正常handoff1pass不覆盖这个失败分支；已派独立ownership增量和两个入口回归，待验证 |

本轮基线：UUIDv7标识3个文件63 passed；OpenSpec strict/all为40 passed、0 failed。T01 activity 1、history 20项通过，T02 generation最终4项通过；S01两个change strict通过。日志在coordinator与对应implementer artifacts，正式业务数据仅在out/tests对应路径。没有跑全量integration；T04修复现生产source history归属，内部runtime_notice执行合同尚待E01闭合。G03所有generator按当前产物版本pin后真实gen:protocol退出0，四个生成根零差异，已独立核验并提交7596a841，绑定测试Python/Node各1passed。G01/U01旧worktree已精确核对所有独有差异均等主树、无进程后回收；外部patch/report/hash保留。

现有服务环境见 [migration-state.json](../../out/tests/temp/2026/10/03/160552-workspace-migration/coordinator/artifacts/migration-state.json)：`BOXTEAM_PROJECT_ROOT` 为原路径入口，`BOXTEAM_HOME` 为该入口下 `out/development-runtime/boxteam-home`，`BOXTEAM_DEV_PORT_OFFSET=16`；命令 cwd 使用数据盘物理根。避免误查另一 unit 或重复启动服务。

每次派单、换任务、报错、审查、集成和回收后更新本节；每个活动任务登记 `编号 / agent / 已核实模型与 effort / 状态 / 基线与分支 / 独占文件 / 绝对 workdir / 产物及业务工作区 / 验证与提交 / 阻塞与回收`。无任务时不创建占位 agent。OpenSpec 台账由一个指定写入者按证据更新。

审查不通过时保留原业务断言，追到唯一权威合同与真实调用链；不能把现实现已丢失的字段直接解释为设计，也不能只凭旧测试强留兼容。数据清理授权不能用于绕过当前代码刚生成的真实缺陷。归因先读原始失败集合：fresh writer 的 NOT NULL/owner 错误不是旧 fixture；同平台短路径成功也不能把真实 SQLite VFS 预算不足归为环境噪声。主代理统一排审查队列，subagent 只向主代理提交复审请求，避免多个发送者串改 reviewer 当前任务。

实际事故出现后修订对应条款并引用证据；已关闭的历史详情放入已有报告，本节只保留恢复所需事实，避免技能随每轮日志持续膨胀。
