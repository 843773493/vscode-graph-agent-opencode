# 目录用途

镜像目标侧 Node Extension Host 的运行时绑定与基础设施单元测试。

# 可修改内容

- 专用 runtime manifest 的合法绑定、能力缺失和错误配置拒绝。
- Backend宿主owner的并发准入、关闭竞态与失败保留；实际进程只在集成层启动。
- 配置测试经真实 owner 初始化目标文件，保留真实磁盘、无覆盖发布与异步 watcher；只替换宿主进程连接边界，不以手工配置 metadata 替代初始化证据。

# 不可修改内容

- 不启动真实宿主进程，不以单元测试替代 Gateway 或扩展激活验收。

# 规范

- fixture 隔离全部文件和环境；真实进程验证归 tests/integration/workspace_services/extension_host。
