# 目录用途

实现共享主工作树、固定 dev/release 和日期实验 checkout 的 Git 操作与本地配置复制。实例路由位于上级 `workspace.mjs`，发布基线位于 `scripts/release/`。

# 可修改内容

- Git 注册定位、创建、增量同步、配置复制、依赖初始化、回收与实例参数。

# 不可修改内容

- 不操作真实业务 Home 或自动认定用户验收，不覆盖他人的暂存和未提交内容。
- 不实现 Agent 业务、数据库迁移或第二套开发服务管理器。

# 规范

- 使用 Bun/ESM 与既有原生文件锁；所有 Git 操作显式绑定 checkout。
- 测试只在 `out/tests/scripts/launch/worktree/` 下创建 Git fixture，始终有外部超时和内存保护。
- 目录及同步冲突先检查后写入；配置只按清单复制，不输出值。
