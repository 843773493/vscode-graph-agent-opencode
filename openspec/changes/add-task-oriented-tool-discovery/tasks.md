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
- [ ] 3.2 实现冻结快照上的确定性任务排名、精确 ID 结果、逐项错误和绑定目录／策略／查询的 cursor。
- [ ] 3.3 实现候选数和字节上限、完整 schema 保真、定义过大与不支持引用的明确错误；不截断 schema 冒充完整结果。
- [ ] 3.4 接入 ToolPolicyResolver，分离 model_visible 自动注入与显式发现，阻止硬执行禁用目标通过发现绕过策略；区分已知未授权与未知目录。
- [ ] 3.5 将默认全量摘要改为有界能力导航，保留用户显式 model_visible 自动注入及 Skill 调用；固定 Provider description 不枚举动态目录。
- [ ] 3.6 将发现结果和定义 provenance 接入既有 ToolResult／资源存储及 CSM/Saver 恢复，压缩后恢复必要完整定义，已有正文不重复注入。
- [ ] 3.7 验证搜索→读取完整定义→信封执行真实链路、分页冲突、策略变化、压缩／恢复、删除提示和 turn/model_call 边界；核对真实序列化请求的 ToolSet／epoch 行为并完成静态分析。

## 4. MCP 执行 handle 与连接代际收敛

- [ ] 4.1 在唯一 MCP owner 内建立每 Server 所属生命周期 task，统一创建／关闭 SDK session、完整 relist、通知及过期结果 fencing。
- [ ] 4.2 分离 catalog revision、connection generation 和 invocation lease，冻结精确执行引用；同名同 schema 新连接不得接管旧调用。
- [ ] 4.3 将 Agent 长期捕获工具对象及 initial/live 两段 resolver 全量迁移至 owner 解析，物理删除旧解析路径。
- [ ] 4.4 实现有界的已封存调用执行资格及 HITL 等待保留，区分普通替换、禁用、断开和最新权限撤销；退役后不授予新的旧代际资格。
- [ ] 4.5 统一真实 MCP 结果、isError、structuredContent、媒体／artifact、进度和取消处理；必要时整体替换 LangChain MCP 适配路径并删除旧实现。
- [ ] 4.6 验证同 schema 重连、旧通知、HITL 跨替换、过期 handle、执行撤权、取消与 outcome-unknown；验证同 Backend 多工作区同名 Server 隔离及静态分析。

## 5. 本地连接控制与授权流程

- [ ] 5.1 通过现有本地扩展注册 connections.manage 的 list/connect/status/disconnect，使用稳定工作区／Server identity 与既有工具 operation 幂等身份。
- [ ] 5.2 实现独立的连接／授权操作状态、查询和 deadline；选择现有领域持久化入口及受限凭据存储，不写入导航 catalog 或全局会话目录。
- [ ] 5.3 为受支持 MCP HTTP 授权接入标准 SDK／协议及 state/PKCE 校验；保持 stdio env／HTTP credential reference 现有行为，未知方式明确报错。
- [ ] 5.4 将 Server 授权与上游应用授权分别路由到实际 owner；复用远端已有管理工具，不假设任意 Server 有应用目录或 OAuth 能力。
- [ ] 5.5 在用户授权后真实核验 token／协议初始化／完整目录，再报告 ready；断开撤销新准入并核实资源关闭，失败保留可复查阶段。
- [ ] 5.6 接入产品授权入口与只读状态 API，经 Gateway 透明代理；凭据／短期授权能力不进入模型、日志、SSE 或会话快照。
- [ ] 5.7 验证未连接时本地控制仍可用、重复 connect、错误 callback、授权成功但 initialize/relist 失败、过期、断开失败和凭据输出边界；完成静态分析。

## 6. 持久等待、精确通知与恢复

- [ ] 6.1 复用既有后台工作 handler/store 登记精确 thread 等待、deadline 和 continuation，状态引用按统一会话路径保存，不复制业务参数形成第二事实源。
- [ ] 6.2 实现登记后重读终态与提交后可靠投递，避免丢唤醒；复用后台邮箱和 collect_background_messages，不新增专用等待工具。
- [ ] 6.3 接入既有内部唤醒与执行队列，按 operation 去重、重新检查权限／删除 fence／激活边界，只恢复仍有效意图。
- [ ] 6.4 实现短 status 等待的 child scope 期限，以及长等待取消／过期；取消某 thread 等待不默认断开共享连接。
- [ ] 6.5 实现进程重启对账和无法确认状态；不得恢复旧 socket、重放结果未知外部写入或伪造用户 Turn。
- [ ] 6.6 验证登记前已完成、投递前后崩溃、重复 callback、两 thread 等待同连接、用户输入交错、Session 删除／等待取消／deadline、原 thread 冷重建及静态分析。

## 7. MCP 配置受控热替换

- [ ] 7.1 在既有 Workspace JSONC 配置族和 schema 内补齐必要授权引用及监督参数，遵守既有覆盖层和新增参数命名；无必要不增加参数，不存储凭据明文。
- [ ] 7.2 将变更按 Server 准备新连接，验证命名冲突、完整分页和权限；多 Server 候选全部就绪才进入统一切换。
- [ ] 7.3 接入现有候选持久 applying／受控准入屏障／active promotion，实现配置、目录及执行准入共同可观察的提交边界。
- [ ] 7.4 实现允许并行实例的先建后换与单实例 stdio 的停后再启，分别处理旧服务保留、apply_failed、recovery_required 和切换后清理失败。
- [ ] 7.5 验收逐项安全替换后更新相应配置 policy、诊断与 applied_paths/deferred_paths；保留未知／不支持字段和混合候选的真实重启分类。
- [ ] 7.6 验证低优先级遮蔽、多 Server 部分失败、配置与模型调用交错、切换中崩溃、旧连接排空、共享 Backend 工作区隔离和配置 SSE replay；完成静态分析。

## 8. Web 与真实产品链路验收

- [ ] 8.1 在主窗口会话流程展示产品授权入口、等待／失败／激活状态；工作区连接管理使用底部面板或既有工作区设置，状态来自后端完整对象。
- [ ] 8.2 完成真实前端类型检查及 `bun run --cwd src/clients/web build`；只修改 Web 客户端，不预写 Electron／移动适配。
- [ ] 8.3 沿实际 Gateway、目标工作区和页面初始化链路执行浏览器操作，验证任务搜索→连接授权→完整定义→真实 MCP 执行；授权需测试 OAuth/MCP Server 实际接收请求，不能只 mock helper 或检查健康接口。
- [ ] 8.4 验证 model_call 下授权后自动恢复，以及 turn 下明确等待激活；验证拒绝／过期／断开错误可见、原 thread 恢复和远程 owner 回调可达性，缺少环境时记录具体未验证范围。
- [ ] 8.5 验证同轮多个信封调用各自确认、取消和结果配对；记录完整定义输入 token、首次调用成功率、发现轮次和真实 Provider 可用时的 cached tokens，不把 mock 当缓存收益证据。

## 9. 回归证据与交付检查

- [ ] 9.1 每个代码切片迁齐声明、写入方、消费者与测试后集中验证；优先相关短检查，再按共享合同和故障结果扩大范围。
- [ ] 9.2 所有测试使用 test:matrix 或进程外 timeout／内存保护；正式产物镜像 tests 路径，临时浏览器与诊断仅放 out/tests/temp/<task_name>/，禁止根目录测试工作区。
- [ ] 9.3 保留准确命令、源码指纹、原始输出、退出码、构建／实际业务阶段和未验证范围；不把 CLI 规划校验、健康接口或局部通过当实现完成。
- [ ] 9.4 审查重复目录／dispatch／writer、旧 schema 指纹、live 同名回退、静默字段丢弃、宽泛异常、隐式授权／重试、无界等待和无关改动，清理当前范围旧实现。
- [ ] 9.5 对照全部新增／修改规格场景核验，逐 requirement 合并并行配置规划；运行 OpenSpec 严格校验后，仅在实际实现和验收完成时勾选任务与归档。
