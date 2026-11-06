# 目录用途

存放 Gateway 与真实工作区后端、受控下游工作区服务之间的集成测试。

## 可修改内容

- Gateway HTTP 路由、导航和 Session Generator 的集成测试。
- Gateway 集成测试专用的下游服务替身。

## 不可修改内容

- Gateway 内部单个类的单元测试。
- 不使用下游替身的完整 Gateway、SSH 或 Docker E2E。

## 规范

- Gateway 本身必须作为真实独立进程启动。
- 下游替身必须作为独立本地 HTTP 服务启动并显式清理。
- 导航 scope 下游装具在启动成功前由 `ExitStack` 管理日志，成功后由返回句柄接管；就绪失败或中断仍释放进程和全部日志，清理失败独立汇总并保留启动异常。诊断引用原始日志路径，不全量读取追加日志。
- 正式产物写入 `out/tests/integration/gateway/`。
- `test_mcp_artifact_management.py` 复用原安装装具，先经真实候选和 uv 发布制品，再从正式 Workbench 命令面板管理。引用由原 source／CAS 建立与移除；确认期间新增引用必须触发真实409，取消与查看不发DELETE，结清后显式重提才物理卸载，不预写安装或连接事实。
- `test_search_provider_lifecycle.py` 通过独立 Gateway 与 A/B Backend 验证原 Provider 的注销、服务关闭和同类别版本交接。`search_lifecycle_app.py` 仅在测试组合中观察原 canonical 查询的准入 worker，以原 Session OS 锁探针核安全结算；关键历史由产品消息入口及统一 model_stream 生成，不新增生产控制接口或替代查询实现。
- `test_federated_search.py` 的六个服务在本文件端口块内复用既有可用端口租约，首选占用时有界选择；所有消费者读取同一组实际端口，逐项登记释放并保存探测，不终止未知进程或改变原搜索断言。
- `test_search_scope_changes.py` 复用现有双后端装具与正式搜索 SDK；经产品创建 Session／保存独有文件正文／删除登记，Gateway 停止后沿原 registry owner 准备有真实 UUID 的新离线目标。核 all 的冻结范围、移除后同名但不同 UUID 的新登记和实际后端分发，不将该控制面准备作为新增工作区管理 UI 的验收。
- `test_mounted_file_targets.py` 复用共享 Backend fixture，经真实注册及整组重启建立双挂载；文件引用、并发读写和未注册目录绑定均经产品 HTTP 入口验证，原生正文不作资源目标。工作区和端口沿当前测试文件隔离，不使用共享开发服务。

`test_gateway_launcher_handoff.py` 通过真实 Launcher、Gateway 和受管工作区运行；`runtime_logging_controls.py` 驱动原服务 HTTP、原 Launcher socket 和真实用户配置 watcher，不准备执行或资源状态。核对五类进程的私有认证、实例／revision 冲突、真实配置热应用、实际 DEBUG 落盘、WARNING 过滤及换代后的旧实例拒绝，观察结果保存到本测试 `artifacts/runtime-logging-*.json`。其 Web 驱动 `clients/web/runtime_logging_diagnostics.mjs` 使用同一现场，经真实 Gateway 初始化并打开日志诊断，产物仍归父测试目录。两个工作区通过真实注册和受控重启共享 Backend，并逐轮核7个实际进程及4个活动资源；这项正常热应用验收不代替真实失败补偿。

该页面使用 `web_workbench_assets` 构建正式 Workbench，由 Launcher 的 Gateway 托管。日志诊断从主侧栏的 Gateway 控制中心进入，按对应 ShadowRoot 内的对话框定位；保留原进程实例、revision 和 request_id 断言。HOME 与产物按运行和 pytest 节点隔离；handoff 后按 Launcher 原有退出期限等待旧进程完成连接排空，不用固定睡眠判断退出。

`test_gateway_logging.py` 使用显式 `runtime_logging_app.py` 测试组合，仍运行真实 Gateway、受管 Backend／Node、HTTP 校验和 SQLite 事务；故障只让发出的前置条件过期，不替换进程状态或写入业务结果。终端验收通过模型回放驱动真实 `exec_command`，同时核 wire、canonical、用户定点详情与工作区专用记录。测试组合不进入产品启动入口，模型响应复用统一 model_stream 资产与编码。
