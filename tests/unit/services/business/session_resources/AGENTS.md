# 目录用途

镜像 `app/services/business/session_resources/`，验证会话关联资源的聚合、控制与清理。

- `test_service.py`：会话读准入、Provider 视图与控制、历史筛选和清理顺序。
- `test_registry.py`：按 kind 分派、重复注册与返回类型校验。

# 可修改内容

- 可以维护上述行为测试及其局部 fixture、fake。

# 不可修改内容

- 不在测试中重写资源 owner 或业务适配实现。
- 不启动真实 Gateway、Backend 或辅助服务进程。

# 规范

- 复用现有资源 owner 装具；断言身份、状态、动作、顺序和错误，不锁定内部调用或 SQL 语句。
- 测试路径随生产职责迁移，同步更新测试矩阵；产物写入对应 `out/tests/` 镜像目录。
- 所有验证带进程外超时与内存保护。
