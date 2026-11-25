# 目录用途

集中存放模型流测试的配置、冻结 Provider 和故障 worker。

```text
model_stream/
├── config.py          回放配置、记录和生命周期
├── frozen_provider.py 冻结 Provider 请求与检查点读取观测
├── compaction_model.py 真实候选测试和静态历史生成器共用的固定摘要模型流与生命周期
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
- 本设施供 Integration 和 E2E 共用；手工 SSE 与真实上游录制均沿现有 transport 回放，不按测试层级另造响应服务。运行产物需注明实际模式和所用 cassette 的来源，不能仅凭 replay 推断是录制还是手工，也不能把实时 record 记成回放。
- 配置生成统一接收 `runtime_root` 和 `artifacts_dir`：配置写入当前运行的 runtime，录制写入当前产物根的 `model-stream/`；不得从文件级输出根另建共享配置。`model_stream_replay`沿用同一归属。
- 调用方按 transport 的真实生命周期选择产物根：模块共享 transport 使用 `context.run_root / "artifacts"`，用例独占 transport 使用 `context.artifacts_dir`；不得把模块共享录制挂在首个用例下，或直接写入不区分 run 的 `context.output_root`。
- worker 只作为独立进程入口使用；Python 命令通过 `uv` 和项目虚拟环境解析。
- SDK 与 HTTP handler 统一从 `app.runtime.litellm_runtime` 取得，避免直接 SDK 导入绕过本地目录初始化；真实 HTTP 与崩溃恢复场景仍保留原协议资产、编码及就绪断言。
