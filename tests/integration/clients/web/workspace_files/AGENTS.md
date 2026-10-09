# 目录用途

存放工作区文件树与既有四个集合写入口的浏览器 Integration 测试，以及最小 HTTP 支撑。Chromium 消费生产 intent/Blob 存储、目录和操作 hook；HTTP 使用生产文件路由与真实 WorkspaceService/文件 owner。

# 可修改内容

- 文件 intent、Blob 持久化、刷新恢复、未知受理结果原 ID 重试、目标工作区冻结和多 tab/因果依赖的集成测试。
- 用于装配生产文件路由、真实工作区 fixture、随机监听端口和测试进程生命周期的 HTTP 支撑。
- 本目录测试的调用与产物定位，不迁移无关旧测试。

# 不可修改内容

- 不在支撑中重写受理、查询、上传、执行或恢复业务语义，不以测试路由替代生产文件路由。
- 不启动官方开发服务，不把根源码或 fixture 源目录注册为业务工作区。
- 不把受控认证/Gateway 边界的 Integration 称为完整产品 E2E；不替代既有 BasicChat 产品验收。

# 规范

- 新 fixture 复制到与测试文件路径镜像的 `out/tests/integration/clients/web/workspace_files/<测试名>/workspace/`；原始输出和浏览器产物在同名 `artifacts/`，端口、进程等控制数据在同名 `runtime/`。
- 受控传输/认证边界在测试与结果中明确列出；文件 owner、SQLite 操作与文件副作用必须是真实生产实现。
- 测试使用进程外超时与数据段内存限制，记录真实退出码；生命周期 owner 回收 HTTP/SSE response、socket、Chromium 与全部测试子进程，失败不可被清理异常掩盖。
- 断言按实际持久结果及浏览器行为，区分 durable acceptance、terminal 和未知效果，不以待确认意图伪报成功。
