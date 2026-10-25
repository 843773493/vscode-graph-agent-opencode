# 目录用途

集中存放模型流测试的配置、冻结 Provider 和故障 worker。

```text
model_stream/
├── config.py          回放配置、记录和生命周期
├── frozen_provider.py 冻结 Provider 请求与检查点读取观测
└── crash_worker.py    独立模型流故障执行入口
```

# 可修改内容

- 模型流回放配置和测试专用 Provider 前提。
- 由 pytest 显式启动的模型流崩溃实验 worker。

# 不可修改内容

- 不改变产品 Provider 的请求契约或模型业务行为。
- 不放模型流测试用例或测试运行产物。

# 规范

- 回放及崩溃场景的产物目录由调用测试显式传入，并遵守 `out/tests/` 镜像布局。
- worker 只作为独立进程入口使用；Python 命令通过 `uv` 和项目虚拟环境解析。
