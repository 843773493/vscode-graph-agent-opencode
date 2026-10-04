# 团队协作实时状态与实测改进

主代理独占维护；稳定规则见 [共同流程](team-collaboration-workflow.md)。更新时间：2026-10-05 05:51（北京时间）。goal **active、无预算上限**，交接及七个 change 的实施、独审、验证、集成、清理与技能实测尚未完成。已关闭调查留在 Git 历史与对应报告，不在恢复时重复泛读。

## 当前授权与唯一实现者

用户授权架构熵减、唯一 owner、显式身份、移除旧兼容；开发生成的新旧中间数据可删除，不再询问。保普通源码、未集成独有改动与来源不明文件，不删数据掩盖 fresh writer 缺陷。当前用户要求由 **一个 `gpt-6.1-sol / max`** subagent 贯穿实现；Luna 实现者均停写，只读独审保留，不为旧人数模板增派实现者、不擅换模型。Sol 实际 `turn_context` 已核。

- 物理 ROOT：`/data1/hyf/20260822_agent/vscode-graph-agent-opencode`；旧 `/data/hyf/20260629_agent/vscode-graph-agent-opencode` 是软链接。
- TEMP：`ROOT/out/tests/temp/2026/10/04/024121-team-execution`；WT：`ROOT/out/worktrees/2026/10/04/024121-team-execution`。以下证据相对 TEMP。
- Sol 唯一运行源码根：`WT/thread_owner_implementer`，当前完整运行 baseline **`4cf1d8ddf43c7c32ce27f883e8848e72c383e6e0`**；fork P2窄包已通过strict与增量独审，同一Sol正在接续Runner。状态 index：`TEMP/coordinator/git/sol-trial-status.idx`；产物：`TEMP/coordinator/artifacts/sol-efficiency-trial/`。
- WT HEAD `ad093e0cdee2ca1225b1b9086db5e1d2c6b6b316`；**实际接手完整 preimage `e8141e9cd9677a6e3d45907526444d26baa59440`**。不是中央 e16 或 WT HEAD。每 shell 绝对 index/workdir/编辑路径，固定对象检索，索引隔离不隔离 live。
- 主唯一串行提交者，index `TEMP/coordinator/git/integration.idx`；fresh read-tree → 精确 stage → guard record → 无 pathspec commit → verify → 双祖先。禁 shared index、amend/reset/rebase/push。测试必须 matrix 或进程外 timeout+4GB，uv/bun；代码静态，Web 改后 build。raw 按 candidate/attempt 保真，不覆盖失败、不补造、不为补日志复跑已绿。
- 三保护路径：`app/gateway/control/generators.py`、`app/services/business/session_generation/service.py`、`examples/demos/Itemized_context_storage/`。不新建整树副本/archive/.venv，不 `/tmp`，正式 workspace 镜像测试路径、fixture 源只读。

已裁定：`assembly_ref=ResourceIdentity`；寻址拒绝码 `unsupported_view`；Gateway 保留 ID `current` 创建拒绝；config layer=逻辑来源 `runtime_override`，precedence 不变，active/pending 分开。delegate 复用 Session SQLite publication ledger 同事务，不造 Team JSON 权威。internal admission 无 Turn/accepted ingress，保真实 execution link。详见 `coordinator/artifacts/owner-decisions.json`。

## 主树、候选与保护事实

主 HEAD 每次运行时实际核验，不在本文维护自引用hash；最后源码集成 `68b99261022f85aff7c4dc66811c2b4ebb8c73fc`，T05a 三路径通过独审与定向验证。O02a、Gateway、UUID、配置和寻址等已审绿按依赖复用，原证据不重跑。中央相对主仍609路径未集成，不能将Sol11文件直接套旧主树。只读 `central-dependency-review/attempt-1/report.md` 只确认B01/B02/J01、T05a、C01/C03已集成及fork/E01既知阻塞，未提供其余609路径的精确最小闭合清单；不能据此声称所有前置已审完或可全量覆盖。

主唯一未知源码 diff：`app/services/infrastructure/rollout_context/checkpoint/boundary/fork_boundary.py`，blob **`928f17eed7f50d14f101eee67e8e9ecb9894b0d1`**。原字节保留，主尚未改写或提交；最终组合须独审其 owner/connection 语义，不静默覆盖。

中央完整候选 **`e16c5679002730a2fcf766a04d104be7e207da73`** 尚未全量主集成。e814 相对 e16 有131个既有差异，含旧导航 actor、同步 DELETE、标题/DTO 回退及3依赖软链接，不能把 Sol live 整树替换主树。主对象准备：fork42路径 prelude `58de7dd1d6fd2ae466084b69d277357a5ef1fff6`；当前协作/交接、9.5撤勾与原未知源码保留后为 **`22b6248b50ed4e806fa090f6095d1b52e7a7a012`**。strict 重建相等，未写运行源码、未 runtime 验收；42不是最终白名单。证据 `sol-efficiency-trial/{fork-integration-preparation,fork-root-preservation}/`。

较新主 HEAD 的 OpenSpec 文档仍含中央已移除的旧 catalog/UUID 迁移承诺。第一次整包文档保留被 strict EOF 空白拒，复核也发现合同回退；第二次仅保当前协作/交接与9.5未验收状态，保中央已确认的熵减规范。不能按提交时间整包覆盖源码或文档。

实际主 OpenSpec 勾选：itemized51/87（9.5已撤整项）、uuid49/49、unified5/40、sessionVRN20/35、multiworkspace5/34、context21/57、persistent2/21。不据组件绿或可重建包勾整项。

## 当前 fork 与效率试验

Sol 于 **2026-10-04 18:13:52.8585 UTC** 接手写入，主之后补源码量 **0**。原 Luna 前置保留，不归给 Sol；对比完整候选耗时、入口遗漏、返工、等待和主补救，不据不同测试范围给速度倍率。

- 真实 anchor 第4次定向通过：1pass/4.52s，退出文件 mtime 距释放约26.66分钟；前三次失败 raw 保。pinned 删除/释放与 history/full Turn 边界4pass/9.79s。
- 真实 AppContainer HTTP 两例通过；新容器实际 `app.main.lifespan` 的 prepared/target_committed/committed ready前恢复3pass/15.23s。旧 helper 重开不能冒充进程入口；首 collection/dependency 与锁未释放失败保 raw。
- 真实 user acceptance → lost → retry、internal notice、闭合 tool call/result → completed checkpoint 的新增谱系，暴露 full-copy 对 internal 强制 Turn、execution/metadata 引用漏映射，原失败保留，Sol统一修真实映射而未造 Turn/Job。
- 整个 `test_rollout_fork_modes.py` 最终 **47pass/98.58s/exit0**（attempt-3），首轮31pass5fail与定向5pass原raw保。固定 **e814→`f25989c37a27bead6954ab78f627b0e4dfe0c513`**20文件，verify_delivery strict重建相等/作者候选外差异0；20:00:43 UTC释放写入，接手至完整交付106.84分钟，主补实现0，生产净行数+220/测试+601（不宣称全包净减）。独审确认1个P2，尚未验收。
- 真实source删除/claim双顺序与源删后lifespan恢复、proof/publish失败release、三模式递归fork均已在最终47条通过。本地导入用committed journal证明，不伪造native admission；full二次fork暴露SQL checkpoint主键已重映射但JSON core.id仍原值，现同事务一致且新增反证断言通过。`sol-efficiency-trial/race-proof-runtime/`保每轮原失败。sealed model-call/retry assembly及generation mode/pinned/title意图漂移无专门新增证据，9.5等整项仍不勾。
- 原 Luna 同文件17fail4pass/33.80s，同 anchor 当时失败；测试数量与断言合同已变且起始投入不可比，不能计算模型速度倍率。阶段记录 `sol-efficiency-trial/efficiency-phase-{1,2}.json`；原日志 `thread_owner_implementer/artifacts/r51-current-forward/pytest-rollout-fork-modes-r7.log`。

当前 fork 合同：main物理alias=Session根rollout，child=threads/id；一次 source guard 冻结 selector/bytes/control/generation/Turn/execution lineage，释放后 target 只读冻结事实。prepared 私有 target 用唯一 journal materialization capability 提前映射真实 target-local身份，不新 user acceptance/Job/Turn、不用普通 admission writer顶替；prepare before claim，严格复用既有 claim。失败 release/abort，committed 补 proof/retention 后publish，published 不倒删。普通HTTP每次新fork，generation stable key复用；startup按creation records定点恢复，不扫盘。fresh v9 journal四列 source_thread_id/target_thread_id/checkpoint_ns/source_snapshot_sha256，不加旧开发schema兼容。

中央组合 **`ce5950b628a86db61826a71d4d8e05d69eafeaee`** 已strict/AST0，19个Sol源码blob不变，仅API三方保现代导航合同；逐preimage同步唯一WT（92写/3删，3运行依赖symlink物理保但不入tree）。组合真实HTTP/ready恢复/anchor **7pass/29.72s**、Web build0；同步范围Ruff exit1的81诊断全部与22b基线一致，新增0，不把既有红说绿或改异常类型求lint。主未知fork源码928f仍原字节，组合检查已覆盖其入口。证据 `sol-efficiency-trial/{central-combination,central-sync,combined-pytest,combined-build,combined-ruff}/`；效率完整候选 `efficiency-first-candidate.json`。独审 `fork-fixed-review/report.md` 除稳定key请求漂移外未确认新fork缺陷，主串行集成仍未完成。

fork P2窄包 **ce595→`4cf1d8ddf43c7c32ce27f883e8848e72c383e6e0`**11路径已收：同一creation ledger新增不可变request_intent，codec/preimage/capability/生产caller闭合；manifest仍六业务字段，source facts单独冻结。同key原请求漂移在published/preparing均拒绝零副作用；同参/隐式latest/source删除重放通过。主strict重建相等、11blob=作者、AST0、候选外源码0，保护及Runner未变；补丁SHA `f9c5f226299aebedfd9b4b16ea7db60641a4328e196008ed36b72f8e8c252e7e`。证据 `sol-efficiency-trial/{fork-intent-forward,fork-intent-verification}/`；增量独审 `fork-intent-review/attempt-1/report.md` 已通过，原P2闭合，无新增finding，审查者只读固定对象未代跑测试。

必要新增22+迁移2 **24pass/53.15s/exit0**（runtime attempt5），Ruff/diff-check0。attempt1超时124/26dots与attempt2单例faulthandler保留：旧预算fixture的control比rollout长8bytes，条件数学不可达；有限fixture保真实rollout guard后单例1pass0.42s（attempt3）。相关ledger attempt4的331pass复用，唯一失败及全仓另1处旧`create_context_fork` caller在精确接手e814中已存在；两例原目录移动/重启kind等断言迁到真实AppContainer/Saver/fork/navigation/lifespan集成后通过，不恢复旧接口或手造proof。这些fixture接手成本与生产P2返修分开记录，不为补日志重跑47。

主已基于4cf1释放Sol接续Runner，不增实现者；fork独审并行只读对象已完成，主体47与新增24等验证/机械包/窄独审均通过。尚未主集成，sealed model-call/retry assembly和整项9.5仍待；不宣布永久默认模型或速度倍率。当前证据支持同Sol贯穿及按需窄审，不支持重复泛读旧前置报告。效率记录继续单列Luna前置、接手成本、必要返修、主组合与审查耗时，主补实现源码仍0。

## 后续唯一实现队列

精确对象、源路径与失败分组只在 `coordinator/artifacts/sol-efficiency-trial/implementation-queue.json` 维护；恢复读当前条目，不泛读已关闭调查。

1. **Runner setup cancellation**：固定e16只读核实 persist acceptance/admission 的 `to_thread` 在最外cleanup之外，worker可在取消后继续提交；user Job收尾不能覆盖internal outcome。同一Sol从4cf1接续外层cleanup与真实binding读回；真实Saver屏障覆盖 user/internal × writer进行中/admission已提交setup未完两个窗口，不靠mock绿；JobService有界等待不提前终态。显式cancel→cancelled，timeout→interrupted，保护吸收终态，failed但lost/unknown保恢复轴，不造无admission execution/Turn。定位 `sol-efficiency-trial/runner-cancellation-plan.md`，本轮raw `runner-cancellation/`。
   当前阶段回报：真实Job.control重复取消的user/internal四窗口及stream.open等待writer共5例通过；测试暴露Runner无条件裁掉internal `display_content`，已同链修复。completed吸收、failed/lost保留、无admission零造账、真实child启动恢复及typed Trace owner漂移仍在补必要证据，未交固定候选、不能算验收。main重复Job-as-Turn终态写与旧独立SQL移除；recovery user lost入口也须委派同一converge并保原幂等键，不只修internal。
2. **GraphBinding + ThreadRuntime lease**：Graph候选 `6a44a65957afc3561d93dcf920a0b2a77817a148` 独审P2为main selector未进入durable creation preimage/hash；child已审合同复用，4共享冲突未同步。修main冻结与重入核，再唯一tracker.acquire(pair,admission,generation_key,builder)、singleflight、旧lease排空/代scope关闭后换代、真实Runner/lifespan。Graph包不等于runtime lease完成。
3. **WVL**：固定 `519dd1a405ce7042db2539b24760ec6fe3421dca` 从a952严格183path，但main.py:248悬空try被AST拒，未合中央；不采用e814旧导航/DTO回退。原Web1244pass13fail仅转录，6组13失败路径在queue；先生产main/caller合同，再定向，完整matrix仅必要新闭包后。额外整树workspace已保独有源码后删除。
4. **Terminal/Browser**：Terminal首16path在e16，后8path partial保 `navigation_authenticated_scope/artifacts/p05-terminal-owner/lease-forward-attempt-1/handoff.md`；Backend唯一owner/operationlease，Node127本机trust/一次WS grant，Gateway精确禁管理POST/DELETE旁路，真实ThreadRuntimeBinding，API/Agent/UI全调用闭包。随后Browser，不能全表reconcile误清Browser。
5. **Resource activation生产**：底座7path draft、上层唯一registry、持久workspace key/freshmarker、真实execution/modelcall冻结/恢复、原子sealbind，无None/当前源旁路；`coordinator/artifacts/activation-production-wiring/review-plan.md`。
6. **delegate**：唯一Session SQLite ledger与child publication同事务，recovery/factory/startcaller，published不倒删。
7. **最终验证**：PendingRequest手proto thread/nullableTurn核19/20编号后统一gen，JobDTO/typed Job proto也缺生产必填thread_id；check:protocol只lint/build proto，不能证明生成类型和DTO一致。不手改pb2；OpenAPI、strict OpenSpec、唯一完整WebE2E/真实浏览器及产物清理。未全部完成保持goal active。

## 严重事故与保留证据

WVL于17:33 UTC裸read-tree清空共享index。无事故前精确hash备份；按事故前status/历史tree重建3266entries，不能称全部blob精确恢复。`coordinator/artifacts/shared-index-incident/attempt-1/` 保取证。该作者已收窄为精确源码编辑，Git冻结/导出归主，不只重复提醒。**Sol释放窗口**基线 SHA `d541fd806cf0f78241ab421632efa325d4a84805bcb8c5584b3faf14b582ea2d` 与实际index及stage逐字相同（历史安装606c非本窗口比较基线）；`sol-efficiency-trial/shared-index-recheck/attempt-1/`。

21:42–21:51主preflight误检：设置`GIT_INDEX_FILE`后`git --git-path index`返回私有status index，主误将4cf1状态与共享备份比较并回写私有index。真正`ROOT/.git/index`始终d541fd/3266entries；21:51将私有`sol-trial-status.idx`精确还原cbe343/4cf1，源码、refs与共享index未变，解除导出暂停。原错误报告保留但由`sol-efficiency-trial/coordinator-resume-preflight/attempt-2/correction.json`明确撤销；主协调浪费约9分钟，Sol期间继续实现/测试，不能全算实现停工或归给模型。索引定位已改为absolute-git-dir/index，后续真实导出核效果。

目录迁移已释放系统盘约238GB；默认/Drive旧.boxteam、根terminal-manager、可复现副本已按授权清理，独有源码保。目录审查 `docs/handoff/20261003-151500-team-collaboration-directory-review.md`。不复用迁移排除的历史测试工作区，不再复制整树。

服务历史前端8027/Gateway8030；恢复先dev:status，原环境见 `out/tests/temp/2026/10/03/160552-workspace-migration/coordinator/artifacts/migration-state.json`，cwd物理数据盘。BOXTEAM_PROJECT_ROOT/BOXTEAM_HOME保持记录原入口，PORT_OFFSET=16；不擅新unit/重启全组。
