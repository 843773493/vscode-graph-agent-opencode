# 目录用途

集中存放通信测试的返回值 probe、多节点联邦装配和进程外故障注入后端。

```text
communication/
├── probe.py          send/read/wait 的显式结果与调用记录
├── federation.py     Gateway、Workspace 和 localhost SSH 的组合装具
└── fault_backend.py  由隔离进程加载的通信崩溃屏障与诊断观测
```

# 可修改内容

- 未配置结果时显式失败的通信 probe。
- 由测试上下文提供端口、工作区和产物根的联邦组合装具。
- 只在显式环境变量启用时安装的进程外故障观测。

# 不可修改内容

- 不放生产通信业务规则或测试用例。
- 不用 probe 返回值冒充真实 Gateway、Workspace、SSH 或持久 owner 的行为证据。
- 不让故障后端在未显式启用时修改产品进程。

# 规范

- 每个联邦节点的工作区、端口和进程都由调用方上下文显式拥有并释放。
- 独立 Backend 与下游 Gateway 并行启动；上游 Gateway 等下游就绪后再配对。启动失败必须先等所有并行任务移交句柄，再统一释放，不取消尚持有启动线程的兄弟任务。
- 启动前用既有 `E2EProcessLease` 持有 Backend、Gateway、辅助服务、sshd 与隧道的固定端口，所有服务退出后才释放，避免自动出站源端口抢占；不终止未知监听进程或修改宿主端口配置。
- fault backend 必须保留原始异常和真实 owner 的调用顺序，不补造业务回执。
- 新通信测试辅助按 probe、组合装配或进程注入职责放置，并同步更新 `tests/support/AGENTS.md`。

- 调用方可显式提供 `gateway_runtime_root` 将节点控制面隔离到本次 run/node；每个节点的 Backend 与 Gateway 必须共用对应 HOME 和凭据。
