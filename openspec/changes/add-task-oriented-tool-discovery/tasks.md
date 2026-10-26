## 1. 调用链与集成边界核对

- [x] 1.1 全仓检索完整 schema、tool.args 指纹、目录发布、初始／当前目标解析及其直接和间接测试，记录需要同批迁移的权威写入与消费者。
- [x] 1.2 核对进行中上下文、itemized、多工作区及日志 change 的实际落地状态，按 design 的集成边界定位唯一 owner；配置 delta 合并不得覆盖日志变更。
- [x] 1.3 确认新模块归属、公开 typed 合同、现有持久存储与路径解析入口；新源码子目录补齐四部分 AGENTS.md，不新增万能 registry／独立 writer。

## 2. 完整 schema 与校验纵向迁移

- [x] 2.1 统一 MCP 和本地扩展公开 schema 导出、规范化及方言验证；目录、binding、hash、指引和校验消费同一完整契约。
- [x] 2.2 将目录 revision／schema hash 从 properties 摘要迁移到完整 schema，版本化受影响 binding；明确历史可读但不可恢复执行的错误路径。
- [x] 2.3 删除信封无条件拒绝额外字段及字典校验后裁剪合法参数的路径，保留本地 Pydantic 工具既有默认值／转换语义。
- [x] 2.4 用真实发现／信封入口验证 required-only、additionalProperties-only、$defs、嵌套、枚举、组合和非法字段；确认错误与原 tool_call_id 配对且非法请求未到 Server。
- [x] 2.5 验证允许附加字段的参数完整到达真实 MCP handler，保留既有本地扩展行为，移除所有旧摘要身份消费者并完成受影响静态分析。

## 3. 通用 search_tools 与上下文集成

- [ ] 3.1 注册唯一 search_tools 入口和 query/tool_ids 互斥参数模型，接入现有 MCP、本地扩展与连接控制目录；不新增 MCP 专用入口或第二发现链路。
- [x] 3.2 先将每目标完整定义 facet 与父目录 members／lineage 原子发布到既有 Registry，并由 activation／Saver 冻结正文，再实现冻结快照上的确定性任务排名、精确 ID 结果、逐项错误和绑定目录／策略／查询的 cursor；禁止 live 同名补洞。
- [x] 3.3 实现候选数、最终字节／行数上限和完整 schema 保真；大定义复用 ToolOutputStore／artifact，以 inline_complete 或 needs_read＋引用／摘要／provenance 表达，不新增 schema 库或读取工具；不支持引用、物化失败及引用也超预算时明确报错。
- [ ] 3.4 接入 ToolPolicyResolver 完整结果及统一 ToolMetadata，分离软 model_visible 与硬 model_visibility_locked；发现、精确查询、导航和恢复均服从 model_hidden，执行禁用不能经发现绕过；区分已知未授权与未知目录。
- [x] 3.5 将默认全量摘要改为有界能力导航，保留用户显式 model_visible 自动注入及 Skill 调用；固定 Provider description 不枚举动态目录。
- [x] 3.6 将发现结果和定义 provenance 接入既有 ToolResult／资源存储及 CSM/Saver 恢复，压缩后按当前硬隐藏策略恢复必要完整定义，已有正文不重复注入，全部 definition facet 不自动进入模型正文。
- [ ] 3.7 验证首次搜索前与进程重启后的冻结正文、搜索→内联／artifact 读取→信封执行真实链路、ToolOutputMiddleware 裁剪、物化失败、分页冲突、软隐藏／硬隐藏／撤权、压缩恢复、删除提示和 turn/model_call 边界；核对真实序列化请求的 ToolSet／epoch 行为并完成静态分析。

## 4. MCP 执行 handle 与连接代际收敛

- [x] 4.1 在唯一 MCP owner 内建立每 Server 所属生命周期 task，统一创建／关闭 SDK session、完整 relist、通知及过期结果 fencing。
- [ ] 4.2 分离 catalog revision、connection generation 和 invocation lease，冻结精确执行引用；跨 execution／Turn 持有复用 ExternalResourceLeaseLedger 的 mcp_connection 与 typed resource-use，owner 内短期 handle 保留不建第二账本；同名同 schema 新连接不得接管旧调用。
- [ ] 4.3 将 Agent 长期捕获工具对象及 initial/live 两段 resolver 全量迁移至 owner 解析，物理删除旧解析路径。
- [ ] 4.4 实现有界的已封存调用执行资格及 HITL 等待保留，区分普通替换、禁用、断开和最新权限撤销；退役后不授予新的旧代际资格。
- [ ] 4.5 统一真实 MCP 结果、isError、structuredContent、媒体／artifact、进度和取消处理；必要时整体替换 LangChain MCP 适配路径并删除旧实现。
- [ ] 4.6 验证同 schema 重连、旧通知、HITL 跨替换、过期 handle、执行撤权、取消、outcome-unknown 和持久 ledger／短期 handle 的持有释放；验证同 Backend 多工作区同名 Server 隔离及静态分析。

## 5. 本地连接控制与授权流程

- [ ] 5.1 通过现有本地扩展注册 connections.manage 的 list/connect/status/disconnect；贯通首次需授权时的 workspace 启动、owner publication 和必需资源冻结，使同一目录保持本地控制及其它就绪 Server 可达；使用稳定工作区／Server identity 与既有工具 operation 幂等身份，不吞掉未知授权或协议错误。
- [ ] 5.2 实现独立的连接／授权操作状态、查询和 deadline；选择现有领域持久化入口及受限凭据存储，不写入导航 catalog 或全局会话目录。
- [ ] 5.3 为受支持 MCP HTTP 授权接入标准 SDK／协议及 state/PKCE 校验；保持 stdio env／HTTP credential reference 现有行为，未知方式明确报错。
- [ ] 5.4 将 Server 授权与上游应用授权分别路由到实际 owner；复用远端已有管理工具，不假设任意 Server 有应用目录或 OAuth 能力。
- [ ] 5.5 在用户授权后真实核验 token／协议初始化／完整目录，再报告 ready；断开撤销新准入并核实资源关闭，失败保留可复查阶段。
- [ ] 5.6 接入产品授权入口与只读状态 API，经 Gateway 透明代理；凭据／短期授权能力不进入模型、日志、SSE 或会话快照。
- [ ] 5.7 从真实工作区启动及 Agent 入口验证首次 OAuth、多个 Server 仅一个未授权、本地 connect 可达与未知目录不虚构；验证重复 connect、错误 callback、无支持流程的 401、授权成功但 initialize/relist 失败、过期、断开失败和凭据输出边界；完成静态分析。

## 6. 持久等待、精确通知与恢复

- [ ] 6.1 复用既有 typed/versioned BackgroundTask handler/store 登记精确 thread 等待、deadline 和 continuation；观察等待使用原子 mailbox effect，不重复 connect／token 交换／业务副作用，引用按统一会话路径保存，不建第二事实源。
- [ ] 6.2 实现登记后重读终态，在现有控制存储事务中原子提交 checkpoint、稳定 ID 邮箱消息和 checkpoint 内待投递引用；以可恢复投递阶段衔接 RuntimeSourceDispatcher，同幂等 key 重放并结算，不将 emit／collect 当作 Job 准入，不混用邮箱与 wakeup ack。
- [ ] 6.3 原调用先提交配对等待结果并结束，经既有内部唤醒／队列按 after_turn 准入独立 internal execution，turn_id=None 并冻结自身新目录；按 operation 去重，重查权限／删除 fence／deadline，不改写原 Turn 或伪装 retry，只恢复仍有效意图。
- [ ] 6.4 实现短 status 等待的 child scope 期限，以及长等待取消／过期；取消某 thread 等待不默认断开共享连接。
- [ ] 6.5 实现进程重启对账和无法确认状态；不得恢复旧 socket、重放结果未知外部写入或伪造用户 Turn。
- [ ] 6.6 验证登记前已完成、邮箱提交后 dispatcher 提交前崩溃、dispatcher 接受后投递结算前崩溃、重复 callback、两 thread 等待同连接、用户输入交错、Session 删除／取消／deadline 和进程重启后的原 thread 冷重建；确认观察恢复不重放授权副作用并完成静态分析。
- [ ] 6.7 等待展示、取消和对账接入既有 BackgroundTaskResourceProvider；验证运行中仍遵守 idle unload blocker，不另建授权任务清单或未定义挂起状态。

## 7. MCP 配置受控热替换

- [ ] 7.1 在既有 Workspace JSONC 配置族和 schema 内补齐必要授权引用及监督参数，遵守既有覆盖层和新增参数命名；无必要不增加参数，不存储凭据明文。
- [ ] 7.2 将变更按 Server 准备新连接，验证命名冲突、完整分页和权限；多 Server 候选全部就绪才进入统一切换。
- [ ] 7.3 接入现有候选 applying journal／claim／受控准入屏障／active promotion，实现配置、目录及执行准入共同可观察的提交边界；补齐 candidate_applier 返回后才 commit 的窗口，不将两个独立赋值视为共同事务，不新增配置事实源。
- [ ] 7.4 实现允许并行实例的先建后换与单实例 stdio 的停后再启，分别处理旧服务保留、apply_failed、recovery_required 和切换后清理失败。
- [ ] 7.5 验收逐项安全替换后更新相应配置 policy、诊断与 applied_paths/deferred_paths；保留未知／不支持字段和混合候选的真实重启分类。
- [ ] 7.6 验证低优先级遮蔽、多 Server 部分失败、配置与模型调用交错、切换中崩溃、旧连接排空、共享 Backend 工作区隔离和配置 SSE replay；完成静态分析。

## 8. Web 与真实产品链路验收

- [ ] 8.1 在主窗口会话流程展示产品授权入口、等待／失败／激活状态；工作区连接管理使用底部面板或既有工作区设置，状态来自后端完整对象。
- [ ] 8.2 完成真实前端类型检查及 `bun run --cwd src/clients/web build`；只修改 Web 客户端，不预写 Electron／移动适配。
- [ ] 8.3 沿实际 Gateway、目标工作区和页面初始化链路执行浏览器操作，验证任务搜索→连接授权→完整定义→真实 MCP 执行；授权需测试 OAuth/MCP Server 实际接收请求，不能只 mock helper 或检查健康接口。
- [ ] 8.4 验证 model_call 与 turn 下均能通过有效 continuation 和 after_turn 独立内部执行自动恢复，新执行冻结新目录且原 Turn 不变，无需用户再发继续；验证未过准入时的等待事实、拒绝／过期／断开错误可见、原 thread 恢复和远程 owner 回调可达性，缺少环境时记录具体未验证范围。
- [ ] 8.5 验证同轮多个信封调用各自确认、取消和结果配对；记录完整定义输入 token、首次调用成功率、发现轮次和真实 Provider 可用时的 cached tokens，不把 mock 当缓存收益证据。

## 9. 回归证据与交付检查

- [ ] 9.1 每个代码切片迁齐声明、写入方、消费者与测试后集中验证；优先相关短检查，再按共享合同和故障结果扩大范围。
- [ ] 9.2 所有测试使用 test:matrix 或进程外 timeout／内存保护；正式产物镜像 tests 路径，临时浏览器与诊断仅放 out/tests/temp/<task_name>/，禁止根目录测试工作区。
- [ ] 9.3 保留准确命令、源码指纹、原始输出、退出码、构建／实际业务阶段和未验证范围；不把 CLI 规划校验、健康接口或局部通过当实现完成。
- [ ] 9.4 审查重复目录／dispatch／writer、旧 schema 指纹、live 同名回退、静默字段丢弃、宽泛异常、隐式授权／重试、无界等待和无关改动，清理当前范围旧实现。
- [ ] 9.5 对照全部新增／修改规格场景核验并运行 OpenSpec 严格校验；仅在实际实现和验收完成时勾选任务，不把规划修订或结构校验当作功能完成。
- [ ] 9.6 规格同步／归档前确认 unify-product-runtime-logging 已先同步主规格并归档；重新读取最新主规格，核对两个共享 MODIFIED Requirement 保留 Gateway logging.runtime、仅日志热应用及混合 Gateway 候选场景，再同步／归档本变更。前置未完成或正文漂移时先修订 delta，禁止逆序覆盖，不改动日志 change 的文件。

### 3.6 活动定义恢复验收（2026-10-26）

- canonical 发现结果与 sealed 来源共同派生活动定义，正文及 provenance 沿原 activation 恢复；不新增 CSM 控制行。普通续读不重复注入，压缩后恢复完整定义，变更提供完整替换或删除事实，当前硬隐藏与执行禁用继续生效。fork 复制原发现依赖但不扩大可见前缀，来源登记期间 canonical/view 推进则明确冲突。
- `integration-extension-discovery` 128 passed；既有 `restore-seal-regression-068` 13 passed 的受测生产代码未变，复用原事务回归。新增真实 Runner 验收通过发现、压缩、冷读、fork 后继执行及正式 `edit_and_continue` 回退，1 passed／154.58秒；回退到发现前不恢复后来的定义。模型使用现有 model_stream 资产，不代表真实 Provider 或 Web 验收。
- 准确命令、源码指纹及原始输出保存在 `out/tests/temp/task-oriented-tool-discovery/artifacts/restore-*` 和 `out/tests/temp/tool-definition-recovery/artifacts/verify-20261026-01/`。受影响 Ruff 通过，新增模块与两份测试 Pyright（显式使用 `.venv/bin/python`）零错误；已有扩大类型基线为99条／当前80条且无新增，保留 `restore-type-comparison-064.json`，不宣称全仓类型通过。仅勾选3.6，连接控制与其余综合验收保持未完成。

### 4.1 Server 生命周期 task 验收（2026-10-26）

- SDK 创建、initialize 与退出集中到每 Server 的原 task，LifetimeScope 持有关闭／排空回调；移除外层共享 AsyncExitStack。完整 relist 和原子 publication 仍归原目录 owner，session 子任务失败明确失效目录、保留诊断，关闭失败保留 scope 并拒绝重开。启动与释放同时失败保留两份原始原因；启动取消排空正在 initialize 的 session。
- 同一 Owner 重启时连接 generation 继续推进，不随关闭改写旧代际；旧通知和过期 relist 不影响新目录。此处只是进程内生命周期 fencing，未宣称完成4.2的持久资源／调用资格或4.3的执行 resolver 迁移。
- 最终 `session-lifecycle-regression-final-078` 为136 passed／89.67秒，源码前后无变动；包括真实 stdio 从不同 caller task 启动、刷新、信封执行及关闭，以及 AnyIO 多 Server、取消、SDK reader 故障、启动／关闭双失败和旧通知验收。`session-lifecycle-static-complete-079` Ruff及相关模块／新测试Pyright零错误。命令、指纹、raw及退出码保存在现有 `out/tests/temp/task-oriented-tool-discovery/artifacts/`；不代表OAuth、热替换、真实Provider或Web验收。
