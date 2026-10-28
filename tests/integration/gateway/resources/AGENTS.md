# 目录用途

验证 Gateway 资源目录聚合的 HTTP 合同与下游错误隔离。

## 可修改内容

- 资源查询的真实 Gateway 集成测试与显式下游 HTTP 替身。

## 不可修改内容

- 不在测试内实现资源业务或准备执行、fork、恢复事实。
- 不访问真实用户控制面或工作区。

## 规范

- Gateway 与下游替身均作为独立进程运行，使用 pytest fixture 协调清理。
- 正式产物镜像测试文件路径写入 `out/tests/integration/gateway/resources/`。
- 手工资源数据仅用于查询验证，不作为执行与恢复验收。
