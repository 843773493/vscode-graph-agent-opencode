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

`test_gateway_launcher_handoff.py` 通过真实 Launcher、Gateway 和受管工作区运行；`runtime_logging_controls.py` 驱动原服务 HTTP、原 Launcher socket 和真实用户配置 watcher，不准备执行或资源状态。核对五类进程的私有认证、实例／revision 冲突、真实配置热应用、实际 DEBUG 落盘、WARNING 过滤及换代后的旧实例拒绝，观察结果保存到本测试 `artifacts/runtime-logging-*.json`。其 Web 驱动 `clients/web/runtime_logging_diagnostics.mjs` 使用同一现场，经真实 Gateway 初始化并打开日志诊断，产物仍归父测试目录。两个工作区通过真实注册和受控重启共享 Backend，并逐轮核7个实际进程及4个活动资源；这项正常热应用验收不代替真实失败补偿。

`test_gateway_logging.py` 使用显式 `runtime_logging_app.py` 测试组合，仍运行真实 Gateway、受管 Backend／Node、HTTP 校验和 SQLite 事务；故障只让发出的前置条件过期，不替换进程状态或写入业务结果。终端验收通过模型回放驱动真实 `exec_command`，同时核 wire、canonical、用户定点详情与工作区专用记录。测试组合不进入产品启动入口，模型响应复用统一 model_stream 资产与编码。
