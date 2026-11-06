# 目录用途

验证固定上游 Node Extension Host 的构建、Backend 进程基础设施、原 IPC／RPC、真实扩展激活及目标文件访问。

# 可修改内容

- 明确主线程替身边界的宿主协议测试、进程生命周期测试和测试扩展。

# 不可修改内容

- 不实现产品宿主管理、安装账本或另一套扩展协议；不运行上游测试。

# 规范

- 使用显式 Node runtime 和隔离依赖，产物按测试路径写入 out/tests；所有运行带进程外超时和内存保护。
- 原型不代表 Gateway、目标授权、多目标窗口 registry 或完整产品验收。
- 真实 Backend 模块 owner 的集成不代表已经接入 FastAPI lifespan；测试 TCP listener 仅传原协议字节。
