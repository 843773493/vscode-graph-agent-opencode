# 目录用途

验证本地发布包的安装、运行时资源取得和平台边界。

# 可修改内容

- 真实执行安装或 postinstall 入口的集成测试。
- 对应测试的受限宿主运行器和场景断言。

# 不可修改内容

- 不执行真实全局安装、不访问公共 registry、不覆盖用户安装目录。
- 不在这里修改生产打包实现；生产代码归 `packaging/runtime/` 或 `scripts/install/`。
- 不修改共用进程保护 helper；它由 `app/runtime/windows_job.py` 持有。

# 规范

- 测试原生 Windows 进程语义时，用 Windows Job Object 同时限制进程树的时间和内存。
- 用本地 HTTP 和隔离的 `out/tests/` workspace 验证下载、校验和文件发布边界。
- Linux 上的 Windows 专用 skip 不是 Windows 原生验证证据。
- 保留准确命令、原始输出、退出码和使用的源码快照。
- `test_sdk_protocol_bootstrap.py` 在独立 checkout 和标准旧 SDK 环境验证纯生成、正常安装与应用合同门；按真实构建／安装链归 Integration。`test_backend_sdk_artifact.py` 验正式 application 清单与锁定依赖组成的完整 Backend 初始化，不以开发 editable 补齐制品。
