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
