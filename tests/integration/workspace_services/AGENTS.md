# 目录用途

存放 Browser、Terminal、MCP 等辅助服务与替身外部边界组合的集成测试。

```text
workspace_services/
├── test_gateway_resource_shutdown.py  Gateway整组关闭与资源回收，模型使用显式回放替身
├── browser/   Browser Manager 与受控页面、网络及进程组合
├── terminal/  Terminal 模块与受控系统进程、信号及回收行为
└── mcp/       MCP 客户端与受控协议服务的交互
```

按被测辅助服务选择目录；纯解析和状态计算留在对应模块的单元测试，经过完整用户入口的流程归 E2E。

`test_http_shutdown_deadline.py`通过真实Terminal／Browser进程、HTTP及POSIX SIGTERM，验证正常关闭保持143，不完整创建请求的正文不能无限阻塞已排空Manager的HTTP收尾：默认5秒后明确诊断并退出1，原持久资源集合仍为空。suite `integration-service-http-shutdown`固定60秒／4GiB；空Manager场景不代替资源排空、整个shutdown、Gateway/Web或Windows验收。

`test_http_json_errors.py` 复用原双辅助服务装具，通过真实 HTTP 核对 Terminal／Browser 非法 JSON 的三类原生路径输入只留诊断、不创建资源；随后正常请求仍可创建和删除真实 PTY／本地文件页面。空正文、正常 JSON、分块 UTF-8、原始 cause 和日志失败由共享解析器单测验证。suite `integration-service-json-errors` 固定90秒；Linux按每进程4GiB数据段保护，不代表整树总内存预算，也不代表 Python owner、Gateway／Web 或 Backend 重启验收。

`test_resource_record_errors.py` 经正式 Session、ASGI 资源 API 和真实 PTY/Chromium 验证 owner／lease 落盘失败不发布候选、普通错误不泄露位置、原资源身份与精确占用不变及同入口显式重试。原工具读取生成 active lease，授权账本失败不得派发真实授权，收尾仅释放原 operation。无模型调用，配置仅作用于本测试工作区；`integration-resource-record-errors` 与 `integration-resource-lease-errors` 分别选择记录和授权场景，均固定90秒/4GiB，须串行运行，不代表 Gateway/Web 或 Backend 进程重启验收。

整组关闭测试复用现有 `model_stream` 资产及精确 SessionThread/ModelCall 回放，由真实 Session 和消息 API 生成执行状态；受控 composition 只提供 ID 和观察端口。Chat/Responses 均核对原 Job 期限、实际回复及 owner，资源和进程回收仍由真实关闭入口证明。共享资产的真实 HTTP、同步和异步 SDK 验证归 `integration/harness/test_model_http_assets.py`，业务关闭场景不再持有模型 socket。

同一测试的`shutdown_environment`只临时改写本次隔离工作区配置，并用目录fd提供Unix socket所需的短临时路径：写入前登记原字节恢复，打开fd后立即登记关闭，短路径核验也在释放范围内。准备失败、测试失败或fd关闭失败仍尝试恢复配置，异常沿标准退出栈保留；不改用户服务环境或持久配置。

`test_resource_orphan_sweep.py`复用现有双辅助服务fixture，经正式Session、Terminal/Browser创建/detach、原孤儿回收与显式管理核实，验证同批peer在首项回执等待中完成真实删除，原30秒预算跨撤权和删除回执保持唯一；删除已落盘但Python未收到回执时保留unknown/原lease，核实只对账且不重发删除。通过 `bun run test:matrix -- --suite=integration-resource-orphan-sweep` 运行，固定120秒/4GiB；真实Node、PTY/Chromium与HTTP不替代Gateway/Web、Windows、不合作操作终止或完整shutdown验收。

`test_resource_detail_deadline.py`复用双辅助服务与原ASGI资源API fixture，经正式Session/创建/详情及工具operation生成真实资源和active lease；在真实恢复之后持原资源锁，或暂停真实外部清单回执，只推进本次查询截止，核503/request_id、peer可读、记录与原lease字节、外部身份/版本及显式重试。独立suite `integration-resource-detail-deadline`固定90秒/4GiB，不扩大原资源释放suite期限，不代替Gateway/Web、真实等待30秒或Windows原生资源验收。

`test_resource_grant_deadline.py`经正式Session、ASGI资源创建/传输授权与真实Node/PTY或Chromium/HTTP，验证原资源锁及已签发授权回执等待到期后的503/request_id、peer授权、原身份/版本、精确lease冷读和原管理停止结算。派发前到期可沿原入口重试；回执未知时存活查询不结清，停止核实后拒绝新授权。suite `integration-resource-grant-deadline`固定90秒/4GiB，Linux/Windows均运行；只推进本次截止，不替代真实等待30秒、Gateway/Web、独立Backend重启或整个operation终止核实。

`test_resource_operation_admission.py`复用正式Session、ASGI资源创建及原operation，真实PTY或Chromium/HTTP核对资源锁准入到期、peer继续执行、原记录/lease冷读、外部身份及显式重试；不派发与无后台补做由原transport调用观察证明。suite `integration-resource-operation-admission`固定90秒/4GiB；只推进本次截止，不代表真实等待30秒、Gateway/Web、Backend重启或完整operation终止核实。

`test_resource_thread_admission.py`复用正式Session、ASGI资源创建、原工具operation及导航enqueue/唯一worker，验证Thread准入503/request_id、原记录/lease不变、显式重试、同Session peer并行，以及逻辑删除先提交而原holder退出后才资源清理/物理隔离。同步I/O场景还覆盖active、Session目录、main/Thread定位、共享catalog连接锁与线程池排队，取消/到期后原gate及holder等真实结算，外部HTTP仍响应且无迟到创建。suite `integration-resource-thread-admission`固定90秒/4GiB；真实PTY/Chromium与HTTP不代表Gateway/Web、Backend重启、原生线程可终止或整个operation终止期限，Windows Browser的内存前置条件另记未验证。

`test_resource_cold_management.py`经正式Session/ASGI创建、detach及脚本执行，冷建原owner并取消已发送版本的普通恢复，再由原stop/delete API释放真实context并核实原脚本结束、当前版本与终态。首次清单场景仅暂停真实HTTP回执，核Terminal/Browser peer独立恢复、迟到清单不重复推进、原PTY/页面身份及冷账本。suite `integration-resource-cold-management`固定60秒；Linux按每进程4GiB数据段保护，不代表整树预算。不手填资源或执行历史，不代表Backend硬重启、Gateway/Web、Windows或任意脚本强制终止。

`test_resource_cold_sync_retry.py`经正式Session/ASGI资源创建、生命周期detach、真实授权及冷owner验证同步发送前失败与已提交后回执丢失。普通查询保留unknown、peer继续可用，重复显式管理失败不造新占用，沿同一冻结版本恢复后只派发一次停止/删除并核实真实PTY或Chromium终态、原lease结算及request_id。suite `integration-resource-cold-sync-retry`固定90秒/4GiB并使用原集成独占闸门；不代表Backend进程硬重启、Gateway/Web、Windows或整个operation/shutdown期限。

## 可修改内容

- 真实辅助服务模块、测试页面服务、mini MCP 和明确协议替身的组合测试。

## 不可修改内容

- 不要把这些用例标记为 E2E 或依赖用户真实工作区。

## 规范

- 目录名使用 Python 可导入的 `workspace_services`。
- 替身边界必须在测试名、fixture 或代码中显式可见。

`test_resource_release.py`沿正式Session、资源创建、operation及原Session资源drain生成状态，复用双辅助服务fixture。真实PTY/Chromium/HTTP核全量lease冲突不部分detach、peer完成后可查询、一次30秒覆盖撤权与删除回执、原lease及同入口核实不重发删除；operation收尾场景推进本进程单调时间，核锁等待到期、健康占用已释放、精确原lease保留、PTY/页面身份和版本不变及同operation显式重试。suite `integration-resource-release`固定120秒/4GiB。Thread入口由共享owner单测验证，不把该场景称为整个Session删除、runtime卸载、Gateway/Web或Windows验收。

`test_resource_release_errors.py`复用同一服务/API fixture，经正式Session及ASGI创建真实PTY/Chromium；真实删除后注入一项回执读取失败并暂停另一项回执，推进原30秒截止，核原错误及超时同时可见、健康项保持完成、精确lease冷读及同入口只核终态而不重放删除。operation收尾另核释放失败后取消仍保留原取消消息和错误cause、健康占用已释放、精确未释放lease冷读、外部身份/版本不变及同operation显式重试；解锁不后台补做。suite `integration-resource-release-errors`独立固定60秒/4GiB，不增加原释放suite预算；多错逆序完成仍按输入顺序归属由共享owner单测覆盖，不代替Gateway/Web、Windows、Backend重启或不合作终止验收。
