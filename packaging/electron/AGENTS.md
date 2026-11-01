# 目录用途

构建与打包只承载现有 Web 工作台的 Electron 桌面容器；发布入口位于 scripts/release。

# 可修改内容

共享 renderer 构建编排、最小宿主 bundle、固定 Electron 运行时打包、便携归档与摘要。

# 不可修改内容

不实现会话、文件系统、网络或 Agent 业务；不增加原生文件系统 IPC、原生扩展或系统能力。不依赖开发服务器或源码目录启动发行物。

# 规范

ESM；依赖通过 bun 安装。默认产物统一写 out/packaging/electron；正式测试通过同一发布入口的 `--output-root` 将构建、下载缓存、bundle 和归档全部写入该测试镜像的 runtime。下载固定版本并校验摘要。保留上游许可，失败保留明确退出信息。真实验证必须从迁移后的产物启动并经过已有 Gateway。
