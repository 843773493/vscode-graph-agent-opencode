## 1. 契约与实现边界

- [x] 1.1 核实 Gateway／Workspace 实际版本、release 支持基线和并发修改，登记本次目标版本；全仓列出固定层加载、defaults／overrides、no_proxy、隐式环境代理及其直接／间接消费者，区分源文件格式与现有业务 API 投影。
- [ ] 1.2 在原配置／协议 owner 定义节点信封、settings 命名空间、单父引用、config_ref、inspection 来源字段和 schema merge 注解；明确原密钥引用、Agent 参数、ModelReference 与封存合同，完成相关真实类型／schema 检查，不新增独立配置服务。

## 2. 唯一节点解析器

- [x] 2.1 在 configs 的聚焦模块实现文件及内嵌节点解析、声明文件相对引用、规范化来源身份、限定别名和 owner／workspace 绑定；覆盖缺父、符号链接等价循环、跨域拒绝、同名 profile 和继承引用基准。
- [ ] 2.2 将递归合并、数组替换、schema 原子块、合法 null、显式 false／0 与最小本地覆盖收敛到唯一算法；参数别名先归一逻辑键、最终按叶子协议映射，header 规范化保留原文来源。同节点别名重复拒绝；对象参数值按 schema 整体替换并明确旧持久参数递归行为的变化，删除旧路径特例的调用方后下线旧逻辑。
- [x] 2.3 产出同一不可变结果的有效值、实际父边和字段来源；候选内节点／文件只解析一次，不为每个模型深拷贝或重复校验整个工作区。用定向测试区分显式等值覆盖、恢复继承和原子块空值。

实施进度：Gateway／Workspace 来源加载、来源编辑、模型解析、诊断及现有维护链路已共用 schema 驱动的 `SettingsMerger`，原 `_path` 业务特判已删除。模型目录及 Agent 运行配置读取同一不可变快照，真实 ConfigService 的 20 个 Provider × 10 个 Model 暖读验证覆盖零来源文件读取、零整份配置解码与返回对象隔离。2.2 尚不能勾选：当前正式 Workspace v3 的对象参数递归语义暂时保留，须随 v4 信封、原 writer 和所有消费者完整切换后统一为原子替换；正式代理字段、`config_ref` 和 Settings 接入仍未完成。

## 3. Gateway 配置纵向切片

- [ ] 3.1 迁齐 Gateway schema／模板／安装器／启动入口到显式父链，更新 loader、source owner、inspection 和原 JSONC writer；runtime 不再按固定层序合并，不接受旧格式 fallback。
- [ ] 3.2 将 Gateway 父依赖及候选依赖集合接入原 watcher、source CAS、prepare／apply／promotion、失败／重启状态；验证父指针切换、失败候选修复和 apply 期间父变更，保留 registry／连接身份／租约合同。
- [ ] 3.3 增加代理互斥 schema、环境捕获及密钥准备；将插件目录／包下载和外部 MCP 真正出网路径接入 Gateway 策略，将内部 HTTP／受管远端控制路径明确保持直连；真实捕获验证，不能共享同一隐式环境客户端。

## 4. Workspace 和模型配置纵向切片

- [ ] 4.1 迁齐 Workspace schema／模板／显式工作区初始化／启动入口及读取路径；保留每来源物化和原 runtime override，将后者作为可见 scope 子节点，不保留独立固定优先级表。
- [ ] 4.2 接入完整父依赖 CAS、候选／active 监听和共享文件 fan-out；验证两个工作区不同父链、单工作区冲突、停止后追赶、移除依赖和 provenance-only 更新，不建跨工作区事务。
- [ ] 4.3 将 Provider／Model 目录读写、发现、测试、复制、移除、引用检查和 inspection 迁为稳定 config_ref；迁齐表单／DTO 的实际消费者后删除 defaults／overrides 二次 merge，覆盖自定义模型节点只写一个参数的真实保存链。
- [ ] 4.4 将已有 Agent／调用参数接为受限临时子节点，保持 null 省略、extra_body 冲突、协议切换原子块和流式合同；验证真实序列化、Saver／request_hash／adapter 一致，不放宽 sealed binding。
- [ ] 4.5 迁齐共享 profile 的真实表单写入与复制：默认局部编辑在同一草稿创建子节点并重指 config_ref，明确共享编辑显示已知影响方；复制自有定义、重绑定副本内部 Provider 引用，外部祖先保持，验证原对象不被改写，不克隆整图。

## 5. 外部请求代理纵向切片

- [ ] 5.1 将有效 proxy 和凭据 revision 纳入原模型客户端身份，接入所有现有协议、同步／异步及流式／非流式请求；覆盖目录使用 Provider、草稿使用原草稿、正式执行／回退／embedding 使用真实 Model 节点，不改进程环境。
- [ ] 5.2 将现有网页抓取、Web 搜索及其他第一方外部 HTTP 出口按消费者清单接入 Workspace 策略；对第三方 SDK 使用其真实客户端／代理注入能力，不以修改全局环境替代。复核内部 Gateway、文件、MCP 控制和调试请求仍直连。
- [ ] 5.3 沿原 owner 生命周期切换／释放客户端，验证代理热变更时旧流存活、新调用使用新策略、相同有效路由不无谓重建；失败使用原期限和取消，不加探测、直连 fallback 或代理专用重试链。
- [ ] 5.4 用隔离真实 HTTP 上游／代理捕获验证 environment、explicit、direct、NO_PROXY／bypass、认证脱敏、远端 localhost 语义、多工作区并发和模型回退；验证现有 TLS／CA 行为不因禁用隐式代理读取而丢失。
- [ ] 5.5 接入 OAuth 登录／设备轮询／刷新真实 HTTP 路径的 Provider 策略，以及 SDK 模型元数据的 Workspace 默认策略；复核全局 SDK 客户端不串 owner／路由。扩展原必要凭据批次到代理 password，并验证配置未变时秘密轮换／删除影响后续调用，旧流保持，不扫描全库。
- [ ] 5.6 复用原 record／replay owner，防止 HTTPX proxy mounts 绕过回放 transport；同步／异步及现有 adapter 验 replay 零真实代理／上游连接，record 记录真实有效路由，live 代理命中验收与回放分开。
- [x] 5.7 环境捕获仅输入标准大小写环境变量，覆盖冲突优先级和 NO_PROXY；验证 Windows／macOS 系统代理存在而环境为空时不隐式回退，不新增 PAC 或系统代理读取。
- [ ] 5.8 按插件实例范围绑定 Gateway／Workspace 父节点与可选插件覆盖，复用已有实例配置与凭据能力，不让 Gateway 读工作区文件；未具备注入能力的出口标未支持。验证终端控制直连、Shell 只继承原启动环境，JSONC 不重写用户命令环境，不改 python_exec／条件脚本／ffmpeg。

实施进度：Provider HTTP 工厂已移除 `trust_env` 代理决策入参，统一消费预先解析的 `ProxyRoutes`；同一模型同步／异步池及 SDK 重试共享一次捕获的路由。受控真实代理验证显式认证、环境变更不换出口及录放路径，OpenAI Embedding 已消费相同准备值。5.1／5.3／5.6 仍待正式配置节点、秘密 revision、owner snapshot generation 与全部出口接入，不能以内部工厂测试宣称 Settings／正式代理配置已完成。

## 6. 原生 Settings 与 JSONC 交互

- [ ] 6.1 在既有 Settings 入口接入统一链／树和字段来源，支持 config_ref／extends 编辑与原文跳转；绑定精确 owner，复用原文本模型、草稿、预检和来源 CAS，不另做配置控制台或保存队列。
- [ ] 6.2 接入代理模式、继承恢复和密钥引用控件；根、只读或无本地定义时隐藏无效动作，显示执行机器、真实 active／pending 和未支持的插件／浏览器页面网络范围；补齐中英文现有资源。
- [ ] 6.3 真实浏览器验证本地默认入口、远端对象保持绑定、两工作区共享文件冲突、表单↔JSONC、父链失败修正与窄布局；完成相关真实类型检查及 `bun run --cwd src/clients/web build`，必要时补受影响 Workbench 构建。

## 7. 版本切换、验证与清理

- [ ] 7.1 根据第1组核实来源决定受支持迁移；有真实合同／额外授权才在原 owner 编写有版本 JSONC 转换与备份恢复，预检报告旧模型双重优先级转换中可能固定的值及取舍。无发布基线不猜测开发数据版本、不改真实 Home，保留已有历史迁移及其支持链。
- [ ] 7.2 更新受影响现有配置技能、AGENTS、说明和测试注释，替换固定合并顺序／两种继承的旧约定；检索清理悬空读写、旧字段 schema、客户端另算来源和不再使用的特殊缓存，不保留双读双写。
- [ ] 7.3 通过实际 Gateway／Workspace／Workbench 产品入口验证配置导入、保存、热加载、冲突和原会话执行；新 Home、真实目录与 ZIP／发行包中的默认来源均可定位。发布平台按实际改动补相应检查，不以单个 helper 成功代替验收。
- [ ] 7.4 复用性能装具验证20个 Provider×10个 Model、共享祖先和六编辑器并存的配置 inspection／模型设置：至少20次暖读 p95≤500ms、单次≤1500ms，可操作模型设置每次≤2000ms，首次未缓存读取≤3000ms；父文件零暖读，不能以 loading 作为终点。记录原始样本、p95／max、数据规模、request_id、源码指纹、命令／退出码及未验证范围。
- [ ] 7.5 所有测试加进程外超时与内存保护，产物沿既有隔离目录；失败先归因，不默认全仓长跑或无变化重跑。复审是否能减少包装、分支、重复状态、深拷贝、重复校验和额外持久化，删除无明确必要性的新增机制，再做相关定向回归。
- [ ] 7.6 增补显式 live 集成用例：从实际验收 Home 的 .env 读取标准代理配置（本机 10809），通过真实 UI／Gateway／Workspace 新建 PTY，无临时 export 或 --proxy 访问外部 HTTPS；Provider 填 base_url、协议、密钥后发现上游模型，再模型测试与正式会话请求，覆盖流式／非流式。复用已有装具，外部调用少量按需运行，不写死测试端口／凭据，不在离线套件暗发付费请求。
- [ ] 7.7 为7.6保留脱敏代理连接／CONNECT 关联证据与 request_id，证明外部各路径命中代理、内部控制直连；隔离故障用例验证原期限和无直连 fallback。登记实际 Home／源码／命令／退出码及未验收阶段，根种子更新、TCP 探测或 mock 成功均不能代替产品验收。

当前实施进度：统一解析器、schema 合并／来源树、代理字段类型及只读环境路由已落地。OpenAI Chat／Responses、Anthropic、OpenRouter、DeepSeek 的认证请求已接入实例客户端；同步／异步及流式／非流式均有真实 SDK 与受控 socket 证据。record 包装实例实际路由，replay 不构造真实 HTTP transport；已迁齐测试注入并删除旧 SDK 全局 session／factory 改写和回放夹具的全局缓存替换。模型池登记到原 Thread LifetimeScope，不增加回收器。5.7 的纯环境解析与系统代理查询隔离已验证，未声称完成 Windows／macOS 原生产品验收。

2.2 仍待旧 owner／模型合并调用方迁齐后物理下线；5组仍待有效 JSONC 策略／凭据 revision、OAuth、元数据、其他现有 SDK 出口和凭据批次接入，5.6 在完整适配范围收敛前保持未完成。3／4／6组及完整 Gateway／Workbench／10809 live 验收未完成，不以当前库级／受控 socket 测试代替。
