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
- 配置生成统一接收 `runtime_root` 和 `artifacts_dir`：配置写入当前运行的 runtime，录制写入当前产物根的 `model-stream/`；不得从文件级输出根另建共享配置。`model_stream_replay`沿用同一归属。
- worker 只作为独立进程入口使用；Python 命令通过 `uv` 和项目虚拟环境解析。
- SDK 与 HTTP handler 统一从 `app.runtime.litellm_runtime` 取得，避免直接 SDK 导入绕过本地目录初始化；真实 HTTP 与崩溃恢复场景仍保留原协议资产、编码及就绪断言。
