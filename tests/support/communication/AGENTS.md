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
- fault backend 必须保留原始异常和真实 owner 的调用顺序，不补造业务回执。
- 新通信测试辅助按 probe、组合装配或进程注入职责放置，并同步更新 `tests/support/AGENTS.md`。
