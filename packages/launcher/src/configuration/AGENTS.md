# 目录用途

存放 Launcher 用户配置初始化、发行运行时发现和 manifest 校验。

```text
configuration/
├── config-bootstrap.mjs         选择用户配置根目录并启动配置初始化
├── config-bootstrap.test.mjs    配置根目录、子进程参数与错误结果
├── runtime-discovery.mjs        按平台选择 runtime 包并加载完整 runtime
├── runtime-discovery.test.mjs   显式 manifest 优先级与平台包选择
├── runtime-manifest.mjs         校验 manifest、解析资源路径并确认资源存在
└── runtime-manifest.test.mjs    manifest 字段、路径解析和资源缺失错误
```

调用顺序是 `runtime-discovery` 选出 manifest → `runtime-manifest` 校验并解析其中资源 → `config-bootstrap` 使用已解析的 runtime 初始化用户配置。平台选择、资源事实和配置根目录各由一个模块持有；新增发行平台时同步更新选择表及对应测试，不在初始化流程里猜测资源路径。

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
