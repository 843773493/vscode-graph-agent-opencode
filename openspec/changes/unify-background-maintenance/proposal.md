## Why

当前工作区把活动事件、配置事件、HTML 快照、未发送附件和上下文详情清理串在一个每小时循环中，任一步失败都会终止整个循环；浏览器孤儿回收另有自己的周期与异常处理。需要统一这些维护任务的进程内执行约束，同时保留现有资源管理器、数据 owner 和启动恢复链路，避免增加第二套资源管理体系。

插件公共管理代码目前散在 `src/shared` 和 `app/runtime`，部分类型依赖 Web 客户端，Python 插件制品还需复制 `app/` 文件。需要建立插件可独立使用的 Node／Python SDK，让插件复用轻量维护调度并脱离产品源码布局。

## What Changes

- 整理资源管理中的“后台维护调度”：从已有检查循环提取轻量的进程内公共代码，固定装配任务，统一周期执行、防重入、错误报告及关闭排空；复用 `LifetimeScope`，不新增独立服务或生命周期框架。
- 建立 `sdk/node` 与 `sdk/python`，分别提供 `@boxteam/plugin-sdk` 与 `boxteam-plugin-sdk`，承接已有实例管理、命令、日志、诊断能力及轻量维护调度；公开入口、内部实现、生成协议分开，SDK 不依赖 `app/`、客户端或具体插件。轻量入口导入不读取插件环境、不初始化管理或诊断；日志静态合同随包交付，按包定位。
- Python 完整生成协议只保留 SDK 内真实 `boxteam/` 包一份，Backend 与插件统一正常导入；迁齐生成工具及全部消费者后删除旧生成目录和 `sys.modules` 别名，不新增协议安装包。纯生成／check 不导入应用或安装 SDK，原必需 HTTP／OpenAPI 合同校验在当前 SDK 正常安装后执行（开发可 editable，发行验收用 wheel），迁齐构建与 CI 顺序。
- AgentExecutionService 将 Thread 闲置扫描的周期触发注册到同一 Python SDK，保留启动时等待首次检查、随后每 60 秒、空执行上下文及原 runtime.shutdown 排空；驻留判断和实际 unload 仍由 ThreadResidencyTracker 负责。
- Thread 维护任务意外退出独立报告，不作为 Job 准入条件；新执行继续沿原 runtime 构建／复用链，仅 owner 关闭及真实执行资源不可用时拒绝，保留原用户取消和关闭错误交付。
- 将工作区已有五项小时级清理拆成独立注册任务，并接入已有浏览器孤儿回收；各任务保持原 owner、周期、清理条件与存储格式。
- Python 维护调度及必要生命周期公共原语只保留在 SDK 一份；统一 request_id 上下文供产品与 SDK 直接使用。明确周期错误与关闭错误边界，在原 scope 内结算已结束任务的错误，避免永久卡住显式关闭重试，不新增关闭状态机或错误账本。Node 采用相同结算约束；终端保留原事件清理入口。
- 单次维护失败明确记录任务和工作区身份及原异常，不中止其他任务；失败任务仅在下一正常周期重试，不立即重试、不追赶积压周期。
- 启动一致性恢复、用户显式删除、Thread unload、终端 steering 和持久任务调度保持各自原链路。维护不替代这些业务动作。
- 明确 owner 指实际负责资源的业务模块：Gateway 管插件进程，插件管内部运行资源，Backend 管工作区业务关联与本地数据；不要求所有服务继承统一基类或各自拥有独立后台。本次不新增跨进程维护 RPC，需要操作远端资源时沿已有业务协议执行。
- 仓库内产品开发通过 Bun workspace 使用 `sdk/node`、通过 uv 标准 editable 安装 `sdk/python`，普通源码编辑不重建归档或改锁；SDK 版本由根版本同步，日志合同从唯一源生成并检查。Node 发行沿原独立运行时清单使用包内 tarball，Python 发行依赖导出排除 editable 项，以正常构建的 wheel 及摘要替换；插件制品继续使用包内归档。
- 开发与 release 共用完整插件目录构建链：SDK 归档放入包内 `vendor/sdk/<内容摘要>/`，用同一依赖声明和标准锁准备依赖，最后才区分目录加载或 ZIP。开发版本依据实际组装目录派生，输入指纹仅用于缓存；不强刷包管理器缓存。同一 Home 同版本不同原制品摘要仍拒绝，目录／ZIP 等价验收使用隔离 Home，不新增有效载荷身份账本。显式应用以原配置绑定为单位，由原 reconcile 交接实例，新工作区默认项单独管理。安装及启动始终不下载、不补装，迁齐后删除旧入口。
- 补充调度、失败隔离、关闭竞态及真实维护入口测试；并发正确性用测试屏障，性能采集独立保持固定数据、批次、硬超时及原延迟预算，单列实际重叠证据，不以重叠次数设通过门，不自动增量补跑或用屏障制造性能样本。

## Capabilities

### New Capabilities

- `background-maintenance`: 资源管理中的进程内后台维护调度、轻量插件 SDK 接入与独立制品、失败隔离、异步关闭排空及进程／owner 边界。

### Modified Capabilities

无。现有附件引用保护、资源保留范围、租约、孤儿期限、URI 发布及业务删除契约不变。

## Impact

- SDK 源码：`sdk/node/src/`、`sdk/python/src/boxteam_plugin_sdk/`；产品 Backend 使用 Python SDK 的唯一维护调度实现，不再新增 `app/core/maintenance.py`。
- 公共代码迁入：Node／Python 插件管理、命令、日志／诊断、唯一 request_id 上下文及必要公共依赖；全仓平移实际消费者。产品 TraceMiddleware、业务 owner、gateway_id／actor 鉴权仍留在原处；原 scope 仅补齐明确的任务终态错误结算。
- 协议与打包：复用 `proto/boxteam/` 和既有生成工具，将全部 Python wire 输出归 SDK；调整 SDK 包资源、标准归档锁校验、开发／发布共用目录构建与启动 bootstrap、插件及产品发行依赖，替换 Python 插件复制 `app/` 的做法。
- 工作区装配：`app/workspace_lifecycle.py`；现有 activity、HTML、attachment、context detail 与 browser owner 的维护入口。
- 内置插件：终端、浏览器、源码调试及 MCP stdio bridge 迁入 SDK 公共入口，保留原业务实现；浏览器接入 Node SDK 周期调度。
- 测试：Node／Python SDK 定向测试、原插件管理协议回归、工作区生命周期、仓库外离线插件制品验收和交互预算检查，复用既有装具。
- 不新增 HTTP API、WebSocket、数据库、用户配置开关、持久任务或资源账本；启动清单支持已有目录与 ZIP 两种受理形式，插件版本和绑定仍归原 owner。不更改前端，不引入 VSIX、VS Code 插件兼容或新的 Extension Host。
- Gateway 自身维护、插件其他清理策略和 federation 清理不迁入中央调度；不要求不同语言共用一份实现或接受中央回收命令。
