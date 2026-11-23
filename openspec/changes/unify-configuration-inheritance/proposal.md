## Why

当前配置同时存在固定文件层级、Provider defaults／Model overrides 二次合并，以及 HTTP 客户端隐式读取代理环境变量的规则。用户无法用一条链解释最终值；同一模型在目录发现、测试和正式调用中也可能采用不同网络出口。需要统一配置继承与来源解释，首先解决多工作区、多模型的代理配置。

## What Changes

- 引入统一的配置节点、单父节点 `extends`、递归合并和来源解析；文件节点和内嵌选项节点使用同一算法。每个有效配置只有一条祖先链，允许多个消费者共享祖先，禁止多继承和按字段选父。
- **BREAKING**：Gateway／Workspace JSONC 使用显式节点信封与命名空间，安装器生成默认父链，运行时只读取指定入口及其祖先，不再按文件名自动叠加固定层。两个配置域继续独立版本化，不把 Gateway 改成跨工作区配置中心。
- **BREAKING**：Provider 和 Model 以稳定引用选择配置节点，替换 defaults／overrides 的隐藏二次合并；内嵌节点定义不参与来源文件的递归拼接。Agent／调用的现有显式参数以同规则的临时子节点接入，不改变模型身份、业务期限和封存请求合同。
- 新增统一 `network.proxy`：显式选择环境、直连或指定代理；缺键继承。代理模式块整体替换，认证沿既有 Gateway 密钥库引用；不隐式修改进程环境，也不在失败时切换为直连。
- 目录发现、模型测试、正式请求、embedding 和 Gateway 自有外部 HTTP 请求使用其实际执行 owner 的有效策略。内部控制面传输保持直连；SSH、浏览器页面和第三方插件流量不得因配置 Backend HTTP 客户端而被宣称已覆盖。
- 插件按 Gateway／Workspace 实例范围选择同域父节点，再按同一规则覆盖；Gateway 启动工作区插件不改变其配置归属。终端命令仍继承启动环境，开发测试在本机 `.env` 显式配置代理，不把业务代理配置静默转换成 Shell 环境。
- 补齐 OAuth、SDK 元数据、代理密钥轮换、录放 transport 和共享 profile 编辑／复制边界；参数按协议无关逻辑键合并，最后映射最终协议，对象参数值整体替换。
- 原生设置入口提供统一的父链／树、字段最终值与定义来源、JSONC 跳转和恢复继承；不新增另一套配置控制台。
- 复用现有快照、CAS、来源文件编辑、共享来源 fan-out、事件与重载分类，将父依赖纳入同一候选基线；普通请求消费已解析快照，不遍历文件树。

## Capabilities

### New Capabilities

- `configuration-inheritance`: 显式单父节点配置图、节点引用、合并语义、来源解释和依赖失效。
- `network-proxy-settings`: 代理策略、执行 owner、模型路径一致性、连接池代次及不支持边界。

### Modified Capabilities

- `configuration-domains`: 以显式入口与祖先替代固定来源顺序，保留配置域、来源 CAS、fan-out 与重载边界。
- `provider-model-configuration`: 替换两种隐藏继承关系，将 Provider、Model、Agent／调用参数接入同一节点模型。
- `harness-agent-configuration-ui`: 原生设置展示一套可理解的继承链、来源和代理配置。

## Impact

- 配置：`configs/` 的模板、schema、安装器和版本声明；Gateway 与 Workspace 的节点信封是结构与语义变更，分别升版。既有 release 来源核实后沿原 owner 的迁移入口处理，不在普通 loader 中修复开发残留。
- 后端：`configs/runtime.py`、`app/core/config_sources.py`、Gateway 配置 owner、Workspace ConfigService／来源与快照模块、`llm_resolution.py` 及 HTTP 客户端创建入口。
- 前端与协议：既有配置 inspection／来源编辑 DTO 和原生 Workbench settings；复用当前配置 URI、错误报告与实时通道。
- 验证：真实配置入口到有效值、父依赖 CAS／热重载、多工作区代理隔离及真实上游捕获；遵守既有总延迟预算，不新增生产审计框架。
- 本机集成门：新建终端通过 `.env` 的 10809 代理出网；原生模型配置发现上游模型、模型测试及正式会话调用均提供代理命中证据。测试地址取环境，不写入产品默认；replay 另验零真实出网。
- 不包含 HTTP 转发代理服务器、VPN、系统代理设置、SSH 代理改造或通用远程配置服务；不改变终端命令、`python_exec`、持续任务条件脚本与 ffmpeg 的执行机制。
