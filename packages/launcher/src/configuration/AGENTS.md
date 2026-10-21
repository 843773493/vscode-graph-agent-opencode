# 目录用途

存放 Launcher 用户配置初始化、发行运行时发现和 manifest 校验。

# 可修改内容

- `config-bootstrap.mjs`：全局配置目录解析与初始化。
- `runtime-discovery.mjs`、`runtime-manifest.mjs`：平台 runtime 选择、manifest 验证与资源解析。
- 与上述模块直接对应的单元测试。

# 不可修改内容

- 不启动或停止 Gateway 进程。
- 不另建配置回退路径或绕过 manifest 解析资源。

# 规范

- 运行时资源必须从显式 manifest 得到，缺少或损坏时直接报告具体路径和发行标识。
- 用户配置根目录由配置初始化模块统一决定，不得在其他模块重复猜测。
