# 目录用途

验证测试运行设施自身的跨模块、真实子进程和本地传输行为，包括进程归属、端口冲突及退出清理。

```text
harness/
├── test_outbox_fixture_lifetime.py          静态HTTP/Chromium装具的准备及清理失败回收
├── test_boundary_display_fixture_lifetime.py 历史边界展示装具的准备与清理失败回收
└── test_manual_browser_fixture_lifetime.py  手动连接装具的Chromium/context/page准备与逆序释放
```

浏览器场景保留在`clients/web/`；这里通过准备边界故障验证它的真实句柄生命周期，不重写业务场景。先观察被测装具的释放结果，再由探针回收仍持有的资源，不能用探针兜底制造“装具已释放”的结论。

## 可修改内容

- 真实进程或本地网络边界组成的 harness 集成测试与专用 fixture。

## 不可修改内容

- 不放纯 mock 的单元测试；它们归 `tests/unit/harness/`。
- 不把 harness 子进程测试作为真实产品用户流程的 E2E 证据。

## 规范

- 通过 pytest fixture 注入依赖，所有测试子进程由创建者确定性回收。
- 只操作本用例创建的进程；端口被占用时保留原所有者并明确失败。
- 高输出接线用例可用真实 HTTP 子进程替换被启动的程序，但保留装具实际输出载体和就绪路径；它证明 Browser、Terminal 装具日志不会阻塞，不证明相应产品行为。
- 辅助服务高输出接线同时使用嵌套工作区和显式正式产物根，验证日志归属跟随调用测试，不随工作区深度改变；同一测试的多个服务共用产物根并以唯一日志名区分。
- 工作区、日志和运行数据写入镜像路径 `out/tests/integration/harness/<测试名>/`。
