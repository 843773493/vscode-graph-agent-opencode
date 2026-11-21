## Context

动机见 proposal.md。总体统称“资源管理”，本次修改其中的“后台维护调度”：整理已有周期检查的执行方式，不新建第二个资源管理器。“owner”只表示实际负责某类资源的模块，不表示所有服务已经共用一个生命周期模块，也不是需要新增的产品概念。

```text
资源管理（职责划分，不是新增统一总控类）
├─ 资源发现与发布：身份、URI、版本、快照                  已有
├─ 生命周期与回收规则：关联、占用、创建、停止、核实和释放  已有各 owner
└─ 后台维护调度：何时检查、防重入、异常报告、关闭排空      本次整理
```

当前没有一个覆盖所有数据与服务的统一检查模块可直接修改；本次从已存在的循环中提取公共调度代码。以下公共机制各有适用范围，不将它们合并成万能管理器：

| 机制 | 已有职责 | 与维护的关系 |
|---|---|---|
| `resource_platform/ResourceRegistry` | 语义资源身份、URI、来源、版本与 immutable snapshot | 不承担物理资源回收，不注册维护任务 |
| `WorkspaceResourceOwner` | 终端／浏览器保留范围、Thread 关联、原 operation lease、核实后释放 | 继续拥有 `sweep_orphans` 及全部回收判断 |
| `ThreadResidencyTracker` | runtime 准入、占用、代际和 idle unload | AgentExecutionService 用 SDK 调度周期检查，tracker 保留全部判断和 unload 阈值 |
| `LifetimeScope` | 进程内任务与回调的持有、取消和释放 | 原实现归 Python SDK；补齐已结束任务错误的关闭结算，Backend 与插件复用，不实现第二个 scope |
| 各数据 owner | 附件引用、保留策略、日志轮转和实际删除 | 维护只调用原入口，不建立资源清单 |

现有周期盘点与本次处理：

| 当前入口 | 时机 | 本次决定 |
|---|---|---|
| `workspace_activity_service.prune` | 启动一次、每小时 | 启动调用保持原阶段；周期部分独立注册 |
| `store.prune_config_events` | 每小时 | 独立注册；保留原 pending candidate 与事件连续后缀保护 |
| `html_snapshot_store.prune_html_snapshots` | 每小时 | 独立注册；保留 owner 的保留期限 |
| `attachment_blob_store.expire_draft_uploads` | 每小时 | 独立注册；保留未发送期限、canonical 引用和 pin 保护 |
| `rollout_checkpoint_runtime.prune_context_plan_details` | 每小时 | 独立注册；保留 sealed／lineage 引用保护 |
| `browser_resource_owner.sweep_orphans` | 启动周期任务后立即一次，随后每秒 | 接入公共调度；原资源锁、lease、范围与孤儿期限不变 |
| `ThreadResidencyTracker.sweep` | 启动时等待一次，随后每 60 秒 | AgentExecutionService 注册 SDK 周期任务；首次检查及原 runtime.shutdown 排空保持原位 |
| `recover_pending_ingests` 等启动恢复 | 按既有启动依赖执行 | 保持原位；不变为可跳过的后台维护 |
| `retry_pending_remote_exports` | 独立每秒循环，非法状态停止并报错 | 属于 durable 业务操作恢复，保持原循环及失败行为 |
| terminal steering 内的 `sweep_orphans` | 原 scan 周期 | 保持原位；本次不拆 steering，不新增第二个 sweep |
| Gateway federation `purge_expired` | 当前检索未发现生产调用 | 记录后续核查项；本次不跨域接入或变更授权数据保留策略 |
| Browser 截图 prune | 插件启动及每小时 | 改用 Node SDK 定时能力；实际截图清理函数留在插件，保持首次立即及原周期 |
| Terminal 历史 prune | 插件启动及创建终端时 | 保留事件触发与原清理条件，不强行改成周期任务 |
| 插件日志轮转及其他内部清理 | 原写入或业务边界 | 公共日志接入 SDK，实际轮转与保留语义不变，不增加通用跨进程清理 |

上述盘点须在实施前再核对调用方，尤其同一共享 worktree 中的新维护入口；不能把未读到的代码推定为缺失，也不能仅为“统一”移动所有后台循环。

## Goals / Non-Goals

**Goals:** 建立可独立使用的 Node／Python 插件 SDK，以小型进程内调度原语消除本次七个 Backend 维护任务与 Browser 截图定时任务的重复调度；公共管理能力迁出产品私有目录，插件安装后不依赖源码目录；让单任务失败可见且不影响其他任务，关闭时不遗弃在途操作，回收资格仍由原 owner 决定。

**Non-Goals:** 中央 GC 服务、通用资源注册表、跨进程调度协议、持久执行账本、任务管理 UI、cron／用户脚本、新的配置热重载机制或清理策略；不实现完整 VS Code 扩展 API、VSIX、新的 Extension Host 或公网 SDK／插件市场。持久任务和用户消息调度不属于后台维护调度。

## Decisions

### 1. 各进程管理自己的资源，SDK 只提供进程内公共能力

| 所属进程 | 负责的事实与动作 | 关闭由谁组织 |
|---|---|---|
| Gateway | 插件进程启动、接管、停止、核实退出，以及自身控制面数据 | 原 Gateway 生命周期；通过已有插件 shutdown 协议请求插件关闭并核实进程结果 |
| 插件进程 | 插件内部 PTY、浏览器实例、连接、文件句柄及内部清理 | 原插件启动／关闭入口负责；公共管理协议委托实际服务关闭，不能把 accepted 回执当作资源已释放 |
| Workspace Backend | 工作区内业务关联、租约、保留规则、数据库及自身任务／订阅 | 原工作区生命周期按依赖顺序关闭；本次在其中装配后台维护调度 |

`WorkspaceResourceOwner` 当前只提供终端／浏览器领域共用的部分生命周期规则，不是所有 Backend 服务及插件统一继承的基类。`LifetimeScope` 提供 Python 进程内持有能力，也不是跨进程关闭器。其他服务继续使用所属生命周期的既有启动／关闭入口；不要求每个服务建立独立 scope、定时器或常驻管理后台。内置插件只平移公共能力调用并接入需要的调度，不重做其业务资源管理。

SDK 的后台维护调度是运行于调用进程内的公共代码。Backend 按工作区装配任务，插件按运行实例装配本地任务，不是独立进程、HTTP 服务、Gateway 代理服务或远程 GC。Gateway 自身维护本次不迁移，后续可按实际需要复用适用实现；不预写宿主 adapter 或多语言通信层。

```text
Backend 本地维护调度
  → 本工作区 Browser owner 的 sweep
    → 核关联、期限和原租约
      → 有必要时沿原 HTTP／Gateway 链路操作插件资源
        → 插件管理内部句柄，原 owner 核实结果并记录

插件内 Node SDK 调度 → 插件自己的截图清理入口（保留原规则）
Gateway 自身维护   → Gateway 自己的数据 owner（本次不变）
```

没有“到点后通过通用 RPC 告知所有服务释放”的广播链路。跨进程只执行已经由具体业务 owner 决定的原业务操作。Backend 连接关闭、Thread 关联释放与插件进程停止是不同动作，保持既有保留策略，不新增因 Backend 重启或检查失败而自动停止插件的规则。

### 2. SDK 按语言分包，公开 API 与宿主实现分开

采用以下实际有功能时才落地的目录，不创建空框架；新增源码子目录按仓库要求补齐四段式 `AGENTS.md`：

```text
sdk/
├─ node/                              @boxteam/plugin-sdk
│  ├─ package.json
│  └─ src/
│     ├─ index.mjs                     公开入口
│     ├─ index.d.mts                   公开类型，不引用客户端
│     ├─ runtime.mjs                   实例身份、管理 HTTP 接入、关闭委托
│     ├─ maintenance.mjs               轻量周期调度
│     ├─ commands.mjs                  命令注册、调用、取消
│     ├─ logging.mjs                   原日志公共接入
│     ├─ observation.mjs               原诊断公共接入
│     ├─ resources/                    构建带入的只读日志合同
│     └─ generated/                    现有 proto 的生成输出
└─ python/                            boxteam-plugin-sdk
   ├─ pyproject.toml
   └─ src/
      ├─ boxteam_plugin_sdk/
      │  ├─ __init__.py                轻量公开入口
      │  ├─ request_context.py         唯一 request_id 上下文，导入不初始化 HTTP
      │  ├─ http.py                    现有请求 ID 依赖及本机控制认证
      │  ├─ lifecycle.py               迁入原 LifetimeScope，统一关闭结算
      │  ├─ maintenance.py
      │  ├─ commands.py                原管理路由、实例身份及命令
      │  ├─ logging.py
      │  ├─ observation.py
      │  └─ resources/                 构建带入的只读日志合同
      └─ boxteam/                      全部 Python wire 生成输出的唯一归属

proto/boxteam/                       唯一跨语言协议源码
app/gateway/                        原安装、进程管理、路由与退出核实
app/services/                       原 Backend 业务 owner
src/workspace-services/              Terminal／Browser／Debug 的实际实现
resources/plugins/                  插件包清单、Skill 与构建依赖
tests/                              SDK、合同及独立制品验收
```

目录按能力逐步增长；超过单文件合理规模后才按真实能力下沉，不先建 `providers`、`adapters`、`managers` 等层次。SDK 不统一封装每种插件的业务入口或资源模型。

借鉴的 VS Code 接缝：`src/vscode-dts/vscode.d.ts` 定义插件可见 API，`src/vs/workbench/api/` 组织运行时实现，`extHost.protocol.ts` 定义跨进程合同，`extensions/vscode-api-tests/` 验证公开入口。本项目仍采用自有服务插件合同，独立服务进程直接加载 SDK，不实现 VS Code 模块注入和 MainThread／ExtHost RPC。VS Code 没有可直接照搬的公共维护调度 API，本能力属于本项目自己的 SDK。

依赖方向固定为“插件／产品消费者 → SDK → SDK 自带依赖与生成协议”。SDK 不导入 `app/`、`src/clients/`、具体插件或仓库绝对路径。公开入口显式列出支持的函数与类型，Node 通过 `exports` 限定公开包入口，Python 从公开包／模块导入；生成 wire 模块不冒充 SDK 业务 API，不向插件暴露私有 helper 作为调用合同。不提供扫描发现、动态 provider、第二注册表或额外结果 DTO。

公开能力按子模块提供。Node 根入口及 `maintenance`、Python 根入口及 `lifecycle`／`maintenance` 必须能在没有插件环境变量、HTTP 服务或运行中事件循环时完成导入，不解析实例／日志去向环境、不注册信号、不启动 timer／task，不传递加载 FastAPI、诊断采集器或管理协议全量模块。管理、命令、日志和诊断从各自公开子模块显式导入，实例状态只在调用原初始化入口时读取；不用根 barrel 重导出全部实现，也不为此增加动态导入代理。原日志 sink 的模块顶层环境解析迁到显式初始化，错误仍在该边界报告，不用空默认值隐藏非法配置。

### 3. 迁入现有公共能力，每项只保留一份实现

Node 的实例管理和命令从 `src/shared/serverPluginRuntime*`、`serverPluginCommands*` 迁入 SDK；插件必需的日志、诊断及控制依赖按真实调用链迁齐。Python 的插件管理、命令、诊断和必要的通用依赖从 `app/runtime` 等原位置迁入 SDK。保留已有主要调用形式与 HTTP 合同，不为目录整理重命名协议字段或增加管理协议版本。

每个公共实现迁入前全仓核对插件、Gateway、Launcher、Backend、测试及构建消费者。仍由 Gateway／Launcher 使用的纯运行库允许它们直接依赖 SDK 公开入口；产品特有的配置、监督器、进程账本、业务 owner、安装发布逻辑继续留在产品。不把产品服务对象搬入 SDK，不保留旧文件 re-export、动态回退导入或复制源码清单。

现有 Node 类型从 Web 客户端生成目录取 DTO 的依赖必须移除；SDK 从现有 `proto/boxteam/` 生成代码并包含传递依赖，沿既有生成／check 流程维护，不手写第二份类型、schema 或协议版本事实。相同 proto 生成不同语言／隔离客户端制品输出不构成第二事实源；同一 Python 进程不得有两套同名生成模块。

Python SDK wheel 同时交付 `boxteam_plugin_sdk` 与标准顶层 `boxteam` 包，后者拥有全部现有 Python wire 生成输出，保留 proto 原包名与生成代码的标准绝对 import。产品与 SDK 一律直接使用该 `boxteam.*`；迁齐全部消费者后删除 `app/protocol/generated` 及 `app/protocol/__init__.py` 的 `sys.modules` 别名注入。产品 `app/protocol` 的 schema／OpenAPI 等业务代码留在原处，只迁其生成模块 import。`buf.gen.yaml`、`scripts/generate_protocol.mjs` 的输出根、包标记、staging 和 check 同批调整为唯一 SDK 输出，不额外建协议发行包，不用 `sys.path`、别名或重复 descriptor 兜底。包标记保持轻量，不在导入包时加载全部 wire 模块。

协议生成与应用合同校验分开：`gen:protocol` 和 `check:protocol` 只运行 Buf、生成输出及原静态生成检查，不导入 Backend、已安装 SDK 或 OpenAPI 导出器，也不安装依赖；check 只比较产物，不回写源码或环境。删除生成前调用 `scripts.export_openapi_snapshot` 的顺序依赖。需要应用代码的 HTTP／OpenAPI 合同检查仍为必需门，沿现有导出器在“生成协议 → 准备当前 SDK → 以正常依赖方式安装 SDK（开发可 editable，发行用 wheel）→ 应用合同校验”之后执行，校验使用本次 proto descriptor 和本次实际安装的 SDK。原构建／CI 检查调用方同批接入这条顺序，不能因纯生成通过就省略应用检查。新增消息或字段时不要求先用旧 SDK 成功导入新 Backend；不允许通过源码路径注入、旧协议回退或手工预装解除依赖环。

Python 的 `LifetimeScope` 原实现迁至 SDK 的 `lifecycle.py`，所有原消费者直接迁至公开入口；必要的现有异步收尾原语按真实依赖一并迁齐。仅按决策 7 修正已结束任务错误永久阻塞关闭的问题；资源释放顺序、失败资源的显式重试和取消保护保持，不复制 scope 或建立维护专用关闭器。维护调度只放 SDK 的 `maintenance.py`，Backend 直接使用，不再新增 `app/core/maintenance.py`。Node 遵守同样调度约束，不强行统一两种语言的同步 API。

Python 的唯一 request_id ContextVar 及现有 get／set／reset／require 迁入 SDK 纯公共模块 `request_context.py`，只依赖标准库。从请求 state 读取 ID 的现有 `get_request_id` 依赖及插件需要的本机控制认证函数迁入公开 `http.py`，保留 FastAPI 可解析的真实 HTTP 类型；该模块不反向导入 runtime／commands。根入口、维护、生命周期和 request_context 不加载 http 模块。产品 TraceMiddleware 仍负责入口 ID 的接受／生成、绑定／恢复、响应头及错误响应；日志、命令、中间件和原消费者直接使用对应 SDK 入口，不保留 re-export。Backend shutdown 业务、gateway_id／navigation actor 及其鉴权校验仍留产品。Node 沿原显式参数传递 request_id，不新建 AsyncLocalStorage。SDK 不补造 ID，不接受可替换 getter／上下文 provider。

原日志等级／热应用状态不另存镜像；清单中的 Skill、工具和配置仍由现有贡献 owner 注册，不在 SDK 再建业务目录。SDK 第三方运行依赖由自身元数据声明，不能靠环境碰巧安装或回落导入产品模块。

`resources/logging/runtime-contract.json` 保留唯一可编辑源及原 version=1；它是既有静态合同，不改名为新配置格式。Python SDK 的包内资源由 `prepare-product-sdk.mjs python` 从该源生成并纳入版本控制，使干净 checkout 的 editable 安装完整可用；`--check python` 只核对版本、依赖声明和资源一致性，不打 wheel 或回写。发行构建仍从同一源带入资源。Node 构建规则不变；生成资源不手工维护。Node 用包模块 URL、Python 用 `importlib.resources` 定位，禁止依赖 cwd、产品根环境变量或向上搜索源码；源码开发也经过同一资源准备步骤，不增加源码路径 fallback。所有实际加载合同的消费者同批迁齐；产品 Home 解析、日志目标与文件句柄仍由原 owner 决定，不把用户可写配置写进 SDK。资源在日志显式初始化／实际使用时加载，维护／生命周期导入不读文件；缺失或不支持版本明确报错。换 cwd、独立制品及原日志热应用均须验证。

### 4. 一个调度原语，静态装配

Backend 在 `app/workspace_lifecycle.py` 使用 Python SDK 装配固定任务，Browser 插件在自身运行实例内使用 Node SDK 装配截图维护。调度原语只需要任务标识、异步回调、周期及是否首次立即执行。Backend 的工作区身份或插件的实例身份由装配传入用于日志，不从全局激活工作区推断；调用注册在本进程的函数，函数不跨进程传输。

最小公开调用形状固定如下，不引入自定义取消 token、句柄包装对象或另一套日志 DTO：

| 语言 | 任务与启动入口 | 取消与关闭 |
|---|---|---|
| Node | `startMaintenance({ tasks, identity, reportError })`；任务为 `{ id, interval_ms, run_immediately, run(signal) }`，回调返回 Promise | 每次装配持有原生 AbortController，回调接收其 AbortSignal；返回 `close(): Promise<void>`，由原 shutdown 持有 |
| Python | `start_maintenance(tasks, *, scope, identity, report_error) -> tuple[asyncio.Future[object], ...]`；任务为 `MaintenanceTask(id, interval_ms, run_immediately, run)`，回调为无参数 async callable；按注册顺序返回原任务供 owner 观察存活 | 调用方传入维护专用原 `LifetimeScope`；通过 Task 取消协作，直接等待该 scope 的 `close()`，不再返回第二个关闭对象 |

`identity` 采用所属工作区／实例现有身份；错误报告回调直接接原 logger，接收任务标识、身份、单调时钟耗时毫秒与原异常，不生成 HTTP 结果类型。两种语言的 `interval_ms` 均为 1 至 2147483647 的整数（排除布尔值），避免 Node timer 溢出导致紧循环；小时和秒只在装配点转换为 3600000 和 1000。Python 内部等待时换算秒，公开 API 不混用单位。

启动先复制并验证整个静态集合的非空／唯一任务 ID、周期、布尔首次策略与可调用回调，同时确认报告入口及原 scope／运行环境可用，再创建任一循环；不通过试执行回调来验证，也不反射推测业务签名。集合中后项非法时，整个启动失败且零回调、零循环。启动后发生异常的原初始化链必须关闭已经持有的 scope／Node 句柄再释放依赖；没有未持有的后台任务。Python 不新增自动回滚管理器，Node 不新增通用生命周期框架。

每个任务一个被显式持有的循环：Python 由原 scope 持有，Node 由原插件 shutdown 持有并等待；不使用每次到期派生新任务的 timer，不使用共享串行队列。这样任务数量固定，同任务最多一次在途，慢任务也不会阻塞其他任务的定时等待。任务完成或失败后再等待一个完整周期，不补跑错过的周期。时间间隔使用单调时钟；数据过期时间仍由 owner 使用原业务时钟判断。

标识仅在一次装配的固定集合内检查重复，不新增进程全局任务注册表、数据库身份或运行历史。同一所属范围只由原启动链持有一个调度集合；重复启动由该原生命周期拒绝，不能创建两套集合后各自声称“集合内无重名”。启动后不提供动态增删、手动运行或第二种调用路径。

拒绝另造通用 scheduler 产品：本次没有用户任务编排需求。Python／Node 通过各自真实回调采用同样约束，不增加语言间 adapter。

### 5. 调度与资源资格正交

回调直接等待原 owner 方法，返回值可供原清理日志使用，不统一包装业务结果。同步 SQLite 操作继续由已有 `run_sync_shielded` 等真实 owner 入口承接，不直接在事件循环同步执行，不额外套第二层线程池。

配置事件清理仍在同一原 worker 中取得 pending candidate、定位保留事件并执行 prune，保留原调用与锁边界；调度模块不自行读数据库。浏览器 sweep 仍调用共用资源 owner，其 `workspace` 资源不因无 Thread 关联而被回收。

即时 delete／stop／detach／Thread unload 不排队进入维护；到期扫描与即时操作的冲突由现有 owner 锁、lease 和版本规则处理。调度模块不增加扫描缓存、lease 或第二套“待回收”状态。

### 6. 单次错误可见，下次正常周期再尝试

循环只在所属 scope 仍开放／Node 停止信号未发出时，将普通回调错误记录为本轮维护失败并等待下一周期；日志包含 task_id、workspace_id／instance_id、耗时和原堆栈。已请求停止时，真实回调错误（包括取消后 worker 返回的写盘错误）直接沿关闭链传播，不能被普通周期错误处理转换为正常结束。Python CancelledError 保持传播；Node 仅将与本次信号对应的协作取消视为停止，不能因 signal 已取消就吞掉 OSError 等无关错误。无立即重试、退避、伪造成功或自动修复。

取消和进程终止类信号不得当作普通维护失败吞掉。Python 调度循环自身意外终止继续沿原 `LifetimeScope` 暴露，Node 沿插件原错误出口报告；不自行重建一套任务监督器。

该策略适用于表内可重新扫描的七个 Backend 维护入口及 Browser 截图周期维护。Browser 当前在后台任务内首次立即清理截图，保留该方式并由同一循环继续每小时执行；首次失败同样明确报告，在下一正常周期再尝试，不新增阻塞插件就绪的 startup gate，不再另执行第二次首次清理。插件其他必须完成的原启动检查、Backend 启动恢复和 fork durable 恢复保持原失败语义。若发现某维护入口不具备重新扫描语义，先修正分类和规范，不能额外添加 `continue_on_error` 开关掩盖问题。

### 7. 等待异步关闭，保留各进程既有关闭顺序

Backend 的 Python 调度使用该工作区独有的 `LifetimeScope` 子 scope 持有任务，不能把多工作区任务放在一个无归属的全局单例。该 scope 是本次维护任务的集合，不代表为每个业务服务新增独立生命周期。接入现有工作区 close 的 periodic 阶段：先关闭维护准入并取消睡眠／请求协作取消，再等待原调用收尾，最后才继续关闭数据库、资源 owner 和 HTTP transport。

scope 正常关闭可复用其原幂等行为；并发关闭不应重复执行回调或创建新的排空任务。已开始的 shielded 同步写入必须结算后才释放依赖。关闭期间出现迟到写盘错误或原释放失败，沿既有关闭错误返回，不伪报 closed，不遗弃给 detached task。调度模块不新增关闭 deadline；复用原业务期限与进程关闭约束，非合作式原生 I/O 无法由协作取消抢占这一限制必须如实保留。

在唯一 `LifetimeScope.close` 中结算任务结果：后台任务的终态失败在关闭前仍由 scope 持有并沿原异常出口报告；一次真实 close 收集已结束任务结果后，从原任务集合移除这些已结算项，同时将原错误完整交给该次关闭调用。该次关闭保持失败，不宣称依赖已释放；后续显式 close 只处理仍未完成的资源／子 scope，不能重放已结束任务、再次清理同一回调或永久重复历史任务错误。资源释放失败仍保留对应资源并按原语义重试，不能因移除已结束任务而删除未释放资源。由原子 scope 向父 scope 逐层结算，不增加 acknowledge API、错误已消费账本或维护私有状态机。并发等待者共用同一次结果，取消等待者不能使 owner 的结果丢失。

关闭在第一次调用时即禁止新执行，唤醒等待并请求协作取消；每次等待结束及调用回调之前再次检查停止状态，覆盖到期与关闭同一轮事件循环的竞态。Node 并发 `close()` 共用本次在途排空 Promise；任务结果全部结算后从原持有集合移除，并把真实错误交给该次调用。失败后仅允许后续显式 close 核实剩余排空，不永久缓存 rejected Promise，也不重报已经交付的终态错误；成功后重复关闭无操作。Python 使用上述唯一 scope 结算规则。两者都保持调度已停止，不重新启动回调、不增加独立错误账本。取消一个关闭等待者不能遗弃原关闭操作；回调忽略取消而稍后正常返回也不能开启下一周期。

Thread 周期扫描由 AgentExecutionService 的专属维护 scope 持有，注册时使用空 Context，不继承请求或执行上下文；维护任务存活与 Job 准入解耦，意外退出独立报告。它不加入 Workspace 六任务列表，关闭仍在原 runtime.shutdown 阶段，唯一关闭任务先等待维护 scope 结算，再清扫描句柄和目录缓存、关闭 tracker；保留每次 30 秒观察、观察者取消不取消关闭 owner、迟到失败先交付及显式重试，不新增关闭状态机。

Agent owner 在 shutdown 入口同步禁止准入，早于异步维护 scope 开始关闭。周期回调须沿该准入拒绝新扫描；已在途扫描若在此窗口失败，错误报告入口须交回原错误，不能因 scope 暂时仍开放而转为普通周期重试并使关闭虚报成功。

维护任务意外退出不阻断新 Job，也不改变其终态：原 scope 报告异常堆栈，任务完成回调补报意外取消或正常返回造成的停止；正常关闭不误报故障。重复 start 保持幂等，不自动重启维护。新 runtime admission 只检查 owner 是否正在关闭，实际构建、lease、blocker 和 generation 安全仍由原 tracker 保障。原 Job.control 用户取消和维护关闭错误交付不变，不新增健康状态镜像或监督器。

新 admission 等待已有闲置卸载期间，若卸载在线性化前因新增 blocker 撤回，应重新检查原 runtime 的 generation 和 scope；同代可用时直接取得 lease，不能将维护撤回作为执行失败。需要换代的 admission 仍须经原卸载与 blocker 检查，真实资源释放错误继续交回等待者，不捕获为正常撤回。

Workspace 六任务的维护子 scope 在早期 periodic 阶段关闭，进程资源平台的后续关闭不能变成第一次停止维护。未迁移的 fork 周期任务继续由原生命周期持有；不能为了删掉 `_periodic_tasks` 就把它混入后台维护调度。

插件使用 SDK 的异步停止／排空入口接入自己的原 shutdown：先停止维护调度、等待已开始的清理结算，再关闭清理依赖和实际业务资源。Node 可以使用原生 AbortSignal 与异步关闭，不为对齐 Python 再造完整 `LifetimeScope` 框架。VS Code 的 `ExtensionContext.subscriptions` 明确不等待异步 dispose，本项目不能照搬此语义；不允许只清除 timer 就宣称在途清理已排空。

维护收尾失败必须沿原关闭链保留并报告，不因首个错误跳过其他原本必须尝试的独立释放步骤；有在途操作时保留其依赖，不新增用日志替代释放结论的做法。SDK 不接管 PTY／Playwright，不拥有插件进程退出决定，Gateway 仍通过原 shutdown 合同和进程事实核实退出。

### 8. 保留启动时机，不引入“就绪先扫全库”

Thread 启动时仍等待首次检查；失败沿原启动日志报告后由正常周期重试，随后每次检查完成后等待 60 秒。tracker 的 30 分钟 idle 阈值、准入、blocker、generation 和 unload 规则不变。

五个小时级任务首次运行保持在周期启动一小时之后；Browser sweep 保持首次立即执行。现有 startup prune／attachment recovery 继续按既有依赖顺序执行，不为每个维护任务新增 startup run，不把全部启动恢复重新执行一次。创建维护任务后不等待周期清理完成才宣布工作区就绪。

清理过程按 owner 的现有查询和锁执行，不在普通 API、模型工具调用或前端渲染前添加完整性扫描。若真实竞争测试暴露 owner 长时间占锁或无界扫描，只针对该入口用现有索引／分批提交能力修正，保持原引用和保留资格；不引入跨 owner 分页协议或全局扫描器。

Browser 截图首次清理保持非阻塞，随后沿每小时周期；其他原启动检查与业务就绪的依赖关系不变，不为统一调度改变失败语义。Terminal 历史仍在原启动／创建边界清理，不增加无需求的 timer。显式删除、stop、shutdown 与卸载继续沿原操作立即处理，不延后到周期维护。

### 9. 开发与 release 共用完整插件目录构建链

开发和 release 的插件都运行构建好的完整目录。沿现有插件构建脚本抽出同一个目录构建函数，调用方只决定加载目录还是将该目录打 ZIP；不另写 release 专用依赖替换，不保留源码直接运行模式。插件／产品显式声明 SDK 依赖；插件开发仍使用构建包，不通过 workspace 链接、Python editable 或源码 pythonpath 运行插件制品。仓库内 Node 产品开发使用 Bun workspace 来源 `sdk/node`，Backend 本地开发使用 uv 标准 editable 来源 `sdk/python`；不改模块搜索路径或建立协议别名，SDK 代码及 wire 仍仅一份。Node 产品发行沿原独立运行时清单携带包内 tarball，不复制根 workspace 声明或源码链接。SDK 的元数据版本和产品依赖版本由准备入口从根 `package.json` 同步，普通源码编辑无须改版本、重建 SDK 归档或更新锁，依赖和元数据变化后重新同步。release worktree／发行 CI 用 `uv sync --frozen --no-editable`；产品依赖导出通过 `export-python-requirements.mjs` 保留锁定的传递依赖和摘要，排除本地 editable 项并加入当前 SDK wheel 及真实摘要，再沿原安装链安装。插件的包内 wheel、锁和完整性合同不变。

```text
SDK 源码 + 插件源码
  → 构建 Node tarball／Python wheel
  → 组装完整插件目录，SDK 包置于 vendor/sdk/<归档 sha256>/
  → 使用同一依赖声明与标准锁准备生产依赖
  → 校验可独立运行的完整目录
      ├─ 开发：Gateway 沿原目录安装／发布入口加载
      └─ release：该目录直接打 ZIP，安装后加载
```

完整目录包括 `plugin.jsonc`、显式组装的插件代码／Skill、`vendor/sdk/`、标准依赖声明与锁，以及 `node_modules/` 或 `python/site-packages/`。构建输出沿现有 `out/` 目录管理，不写回源码模板目录。Gateway 复用已有目录安装／原子发布与依赖校验；release 只封装该完整目录为 ZIP，不能在打包分支补文件、改依赖或重装 SDK。

SDK 归档位于 `vendor/sdk/<归档 sha256>/<标准文件名>`，Node 用 `file:` 相对 tarball 引用，Python 用版本依赖加 uv sources 的相对 wheel 路径；wheel 文件名仍符合标准命名。SDK 内容改变而软件版本不变时，新归档使用新的路径，不覆盖旧归档，不要求清空／强刷 Bun 或 uv 缓存。由包管理器正常更新该依赖的来源及锁摘要，再在新的完整目录中准备依赖。sha256 只用于归档命名与原制品身份，不另存摘要账本；锁仍核包管理器原生摘要。不复制开发 workspace／目录锁，不伪造 registry 或手写锁。

SDK 版本在构建时由根 `package.json` 派生并固化，运行不回读产品根；协议版本不因搬目录升级。沿既有构建缓存分别复用 SDK 归档和依赖准备结果，依赖准备以归档实际内容、声明／锁和目标运行时／ABI 为输入；只改插件代码时不重复安装依赖。完整目录准备成功后先沿原安装入口发布新的不可变制品，再显式应用所选绑定的版本配置，由原 reconcile 停止旧实例并启动 enabled 的绑定。不单独修改 enabled 来编排重启，不覆盖运行目录、不热换 `node_modules`、不重启其他绑定。

干净 checkout 的依赖准备顺序必须可执行：仓库内产品开发先核对／同步两种 SDK 元数据及生成资源，再由 Bun workspace／uv editable 安装；插件和发行仍通过独立打包入口使用生成代码、静态合同源和标准构建工具产出 SDK 包并安装，最后才运行对应启动器。SDK 打包入口不导入 `app`、产品 logger 或待安装 SDK，也不要求先完成依赖 SDK 的产品初始化；协议需要重生成时先走决策 3 的纯生成入口，应用合同校验在当前 SDK 正常安装后执行，发行制品验收仍验证 wheel。将该顺序接入现有 worktree／依赖 bootstrap，不要求开发者临时修改锁文件或手工建立源码链接。清空本任务隔离构建缓存的首次构建验收用于发现这类自举依赖环。

构建输入指纹只作缓存键，不决定插件包版本；它覆盖 SDK／插件源码、生成代码与工具版本、静态资源、基础锁、构建脚本及目标运行时／ABI。缓存命中复用未修改的完整输出；不要求在不同构建环境得到字节完全相同的原生制品，也不以相同输入指纹豁免版本冲突。

开发包版本在所有代码、资源、归档和依赖组装完成后派生：先将清单 version 固定为原包基础版本，以原目录摘要算法计算完整目录，再把 version 写为 `<基础版本>-dev.<该摘要>`。只替换该字段，不再修改其他有效载荷，随后按原安装合同计算最终目录／ZIP 摘要。不把派生版本、自身最终摘要或 ZIP 时间戳再作为版本输入，不新增版本登记文件。不同实际内容获得不同开发版本；同一完整目录选择目录或 ZIP 交付不改变清单版本。正式发布仍使用显式发布版本；原 `(package_id, version)` 不可变规则保持。目录与 ZIP 摘要分别表示原合同中的真实制品，不强求相等；有效载荷比较不含安装 owner 后生成的 `.boxteam-plugin.json`。

保留这一既有交付限制：同一 Home 已安装某 `(package_id, version)` 后，再提交不同原制品摘要必须拒绝；即使目录和 ZIP 的有效载荷相同，也不据此豁免版本冲突。同一来源且摘要相同的重复安装仍沿原幂等行为；不同 ZIP 字节同样不能冒用原版本。开发目录和 release ZIP 默认进入各自隔离 Home，目录／ZIP 等价验收也在隔离 Home 比较；另在同一 Home 验双向切换均拒绝且原安装及绑定不变。不新增有效载荷摘要账本、摘要类型兼容分支、自动卸载重装或绕过冲突的来源探测；需要更换制品时沿原显式新版本安装／绑定更新流程。

SDK 更新只改变相关归档来源及锁记录，其余依赖沿原基础锁约束，不能顺便无约束升级。构建缓存与锁生成属于同一个目录构建流程，不新增锁格式、第三种开发安装器或运行时依赖恢复。

迁齐开发启动闭环：`runtime-manifest` 的内置包条目保留公共身份字段，显式二选一提供 `directory` 或 `archive`；schema、Launcher 解析／路径解析／预检、`config-bootstrap` 和 `configs.installer.install_bundled_plugins` 一起接入已有目录／ZIP 安装入口，不能凭后缀猜来源，不能保留开发“先 ZIP 再解包”的第二构建链。该变化是构建清单增加目录交付形式，不新增用户配置开关或迁移历史 Home。已有同身份制品不能仅因 `store.get()` 成功就跳过来源冲突核实。

版本更新的单位是原配置绑定，不是 instance_id：同一 Gateway 下按配置范围（Gateway 或显式 workspace_id）及 `(package_id, plugin_id)` 定位，Workspace 仍由原配置入口注入 workspace_id；只修改 package_version，保留 enabled、参数与范围。原配置提交／reconcile 负责旧实例停止、交接及新实例启动；构建器不自行再运行一套 stop／start 编排，也不直接写 Home 或工作区配置。禁用绑定更新版本后仍禁用；旧配置 revision 已变化时沿原冲突语义停止应用，不重读后自动覆盖。

新工作区默认项属于 Gateway 配置的独立操作，只影响后续创建，不与现有工作区更新隐式联动。首次隔离开发 Home 的内置默认绑定从本次构建清单取实际版本，经原初始化入口生成；已有 Home 只更新显式选择的绑定。普通启动不批量改配置，未选绑定保留原版本；实例状态仅用于结果核实。构建、安装或配置更新失败时，保留旧制品并报告原配置／停止状态，不启动一个配置未确认的新实例，不新增跨层自动回滚。验收检查配置版本与真实运行版本、SDK 加载来源一致。

现有 `app/gateway/plugins/dependencies.py` 的锁校验、两个插件构建脚本及 `scripts/launch/dev-bundled-plugins.mjs` 必须同批迁齐；目录与 ZIP 安装使用相同规则，仅为已显式声明的 SDK 增加“包内归档”来源，不全面放开本地依赖。Bun 本地 tarball 条目是原生三项形式（身份含相对归档路径、依赖 metadata、sha512），不能硬套 registry 的四项及版本解析；从归档 package.json 核真实 name／version，并与声明、锁来源和已解包依赖树对齐。uv wheel 条目按标准 `source.path`、wheel filename／sha256 解析；根 requires-dist 可能记录 path，须结合 pyproject 版本约束、tool.uv.sources、归档 METADATA 和实际 dist-info 核对 name／version／marker／extras 及来源，不能只比较空 specifier 或跳过根依赖一致性检查。SDK 的传递依赖仍按原 registry 规则核实，其他未知来源明确拒绝。

只允许上述包内普通归档文件，实际解析路径必须留在插件包内；拒绝绝对路径、越界／链接、目录来源、editable、Git 及任意外部 file URL。核原生锁摘要和归档 metadata，沿原依赖树完整性与包发布校验执行，不另建 SDK 摘要账本，也不在普通请求重复校验。正向验收须经过真实打包及安装验证入口；负向覆盖摘要篡改、name／version／来源冲突、缺失归档和越界引用。现有 registry-only 插件保持原规则，不要求它们改为本地归档或自动重写既有安装包。

Python MCP stdio bridge 改为导入 SDK，打包脚本删除逐文件复制 `app/__init__.py`、`app/runtime/*` 等产品模块的清单。Node 内置插件的类型与运行依赖同样迁齐，SDK 不从源码目录、前端 dist 或 `reference_repo` 动态取文件。

插件安装仍只做原校验、复制目录／解包 ZIP、原子发布；安装及启动不运行 Bun／uv／npm／pip 来补依赖，不下载 SDK，不修改包内只读内容。已安装包仍按其明确携带的 SDK 和管理协议合同执行，不从产品目录隐式注入另一 SDK 版本；不要求重写已有安装包，也不自动重打包用户插件。

### 10. 验证以真实失败、公开入口和独立制品为中心

- 单元测试使用可控等待和事件屏障，不真实等待一小时；验证固定间隔、防重入、无积压补跑、异常后下一周期和取消传播，不写纯注册字段快照测试。
- Node／Python 的 SDK 测试从各自公开包入口导入；核原管理状态、命令调用／取消、日志与诊断合同及原 request_id。构建实际包后验证 Node 包 exports／真实类型检查和 Python 导入，不用仓库根 pythonpath 或前端依赖替 SDK 补缺。
- 在没有插件环境变量、没有运行中事件循环且 cwd 改到独立目录的子进程导入轻量公开模块，验证无管理初始化、timer／task／信号注册与重依赖加载；实际创建 runtime 时仍验证必需环境并报错。分别先导入 SDK wire 再导入产品、先导入产品再导入 SDK wire，核模块／descriptor 同一身份及真实消息序列化，不能只断言 import 未报错。
- 在隔离构建环境保留旧 SDK，加入一个由新 Backend 实际消费的协议类型；经纯生成、构建及正常依赖安装当前 wheel，再运行原 HTTP／OpenAPI 合同检查，核实际加载类型与本次 descriptor 一致。纯生成不得导入应用；check 不安装、不写回且能拒绝过期生成产物。新增类型的临时源码只放本次隔离装具，不修改真实业务协议制造需求。
- 两种 SDK 构建后的日志静态资源与唯一源一致，独立制品换 cwd 可读取；经原管理入口热应用日志设置后读取同一实例状态，日志去向、revision、request_id 与原合同一致，无第二份热配置。
- 同一个完整构建目录在隔离 Home 分别经 development directory 和 ZIP 安装入口验收，核有效载荷、SDK 实际加载位置、目标 ABI 与真实业务行为一致；在同一 Home 验目录→ZIP、ZIP→目录的同版本不同摘要冲突拒绝，以及原来源同摘要的重复安装幂等，拒绝后原制品与绑定不变。变更 SDK 后重建，核锁摘要及依赖树改变，旧摘要失败；只改插件代码时依赖准备步骤不重复运行。失败重建不得发布半成品或破坏当前运行目录；不靠覆盖安装元数据放行。
- 使用既有插件构建／安装装具：Node developer-tools 包含终端、浏览器、调试，Python 使用 MCP stdio bridge；把完整制品复制／解包到各测试合法隔离目录，在不提供仓库路径、`PYTHONPATH`、editable 安装或源码目录入口的子进程中断网启动。核管理握手、一个原真实业务操作、停止与退出；不以 import 成功或健康回执代替功能验证。
- “仓库外”验收要求运行环境仅暴露完整制品和受支持的运行时，禁止借用父级仓库 `node_modules`、根 `.venv` 或源码模块补依赖，并核实实际加载来源。工作区、解包制品与证据仍保存在仓库规定的 `out/tests/<同名测试路径>/`；临时探索沿 `out/tests/temp/YYYY/MM/DD/<task>/`，不以验收之名另建非法测试数据目录。
- Browser 的真实 SDK 接入需验证启动清理恰好一次、周期失败后下一周期仍检查、停止后不新执行、在途清理完成后才释放其依赖；Terminal 的 SDK 接入核原事件触发清理，不新增周期。Python 插件注册一个真实本地维护回调，验证独立 SDK 包提供周期能力；无需修改 MCP bridge 的业务规则来制造清理需求。
- 工作区生命周期集成从真实装配入口启动，使用隔离工作区和可控周期；业务 SQL／文件／owner 真实执行。只能替换时钟或指定一次失败的 I/O 边界，不能用 mock 清理函数代替全部业务行为。正式生产周期保持原值，不新增测试专用配置项。
- 一次验收覆盖活动清理失败时附件仍回收、下一周期活动恢复、已接受附件不删、配置保留事件不删、Workspace Browser 不删，以及关闭等待已开始写盘且不释放依赖。跨进程 Browser 可复用既有插件生命周期装具，不为本变更新建插件系统。
- 并发正确性与性能采集分别验收。前者沿真实生命周期启动维护，在测试装具的现有 I/O 边界用屏障确认真实 worker 已开始但尚未结束，期间经 Gateway 读取同一存储，再释放屏障并等待结算；核有效记录与引用保护、正常读取和后续清理结果。只控制测试时序，不替换 owner 或 SQL，不新增生产测试开关；屏障等待不参加性能统计。
- 性能采集固定执行 3 批，每批 10000 条真实待清理活动记录、100 条有效对照，20 次并发暖分页请求与一次真实 owner 清理同步起跑；全部批次执行一次，采集阶段进程外硬超时 60 秒。不根据结果加大数据、补批、自动调参或重试。请求经真实 Gateway 读取同一存储的 `/api/v1/session-catalog/events`（有效游标），不测 health 代替。测试仅记录 owner／请求起止，不改变锁或执行速度。60 个请求全部计入 p95≤500ms、单次≤1500ms 的预算判定，保留失败／慢样本、响应大小与 request_id；实际重叠次数及重叠样本单列，不能只挑重叠或快速样本计算总体预算。预算判定不设最少重叠次数，清理先结束不使达标样本自动失败；没有重叠时明确注明未观察到竞争延迟，不把正确性屏障或无重叠结果冒充竞争性能证据。两项各自报告，任一业务正确性或延迟预算失败均不能宣告验收通过。禁止在性能采集内使用屏障、sleep、mock 慢回调或人工占锁制造重叠。有效记录必须保留，完整响应计时包括内部锁等待。冷读单列沿用≤3000ms预算，不新增全套 UI 测试或产品性能采集器。
- 测试均使用进程外超时和内存保护，保留命令、源码指纹、原始输出与退出码；失败先归因，不放宽预算或通过立即重试过滤失败。

## Risks / Trade-offs

- [多个小时级任务到期产生 SQLite／磁盘竞争] → 固定任务数量、同任务无重入；保留原 owner 串行 writer，并以真实竞争场景约束交互延迟；不先添加全局并发调度层。
- [长期权限或磁盘错误会在每个正常周期再次失败] → 保留明确原异常和任务归属；不静默跳过、不自动删库。该设计不保证清理必成功。
- [同步 I/O 无法被强制抢占] → 等待原 worker 收尾，不能提前关闭存储；不宣称 shutdown 有新的硬期限，不添加远端进程强杀逻辑。
- [复用 scope 后旧周期仍在运行导致双跑] → 接入每项任务时全仓检索所有调用与测试，同一垂直批次删掉对应旧循环；startup 与显式删除调用按其不同语义保留。
- [错误隔离掩盖真正启动损坏] → 仅迁移表中可重复扫描的维护任务，启动恢复和 durable 业务恢复保持原错误语义。
- [抽取 SDK 造成产品与插件循环依赖] → SDK 只承接真实公共运行库与生成协议，产品消费者直接依赖公开入口；不复制 product owner 或保留旧 re-export。
- [源码运行通过，安装制品缺 SDK 或原生依赖] → 真实构建并在仓库外断网验证 Node／Python 插件和产品 Backend 包，沿原目标运行时与 ABI 合同验收；未实测平台明确列出，不推定跨平台通过。

## Migration Plan

不变更业务持久化结构、用户配置版本或已有 HTTP 合同，不需要数据迁移；构建运行清单 schema 增加目录交付形式，标准锁校验增加受限的包内 SDK 归档来源。新增 SDK 公开库入口；先按语言纵向迁齐 SDK、产品／插件消费者、测试和制品，再接入本次维护调度并删除对应旧循环。每个公共能力迁入同批完成旧入口删除，不同时维护 SDK 与旧目录实现。部署沿用现有实例重启和插件发布／更新流程；回退使用前一完整代码与匹配制品，不在当前代码保留双轨开关。现有已安装插件的进程管理合同不因目录重组改变。
