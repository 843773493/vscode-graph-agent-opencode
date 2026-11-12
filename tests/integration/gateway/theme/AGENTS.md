# 目录用途

通过独立 Gateway 进程验证主题图片的公开 HTTP、存储拒绝和显式恢复。

# 可修改内容

- 主题资源的集成用例及专用 pytest fixture。

# 不可修改内容

- 不实现前端页面，不使用开发服务或真实用户数据。
- 不在测试内替产品完成执行、fork 或恢复事实。

# 规范

- 资源通过正式 HTTP 入口创建，不准备游客或普通用户；文件损坏仅作为查询或写入拒绝前提。
- 复用 `tests.support.diagnostic_sink` 注入日志故障，原生文件错误由实际 OS 产生。
- 工作区与产物镜像到 `out/tests/integration/gateway/theme/`，保留集成闸门和进程外保护。
