# 目录用途

验证测试进程设施的启动、HTTP 就绪、日志描述符和确定性清理。用 mock 注入创建失败、中断和清理失败，观察被测装具持有的句柄，不启动真实产品服务。

`test_model_replay_fixture_lifetime.py`只检查 HTTP/SDK 专项测试中模型服务的socket、线程和请求诊断文件；用两种模型协议验证启动与清理失败，不启动Gateway、模型执行或辅助服务，也不替代真实关闭验收。

## 可修改内容

- 进程 scope、Workspace/Gateway 后端和 Browser/Terminal 辅助服务装具的单元测试。
- 组合装具的资源移交、就绪预算和原始异常保留断言。

## 不可修改内容

- 不放真实子进程、监听端口或浏览器操作；这些验证归 `tests/integration/harness/`。
- 不放工作区配置、catalog 生命周期、业务协议等待器或产品实现。

## 规范

- 通过 pytest fixture 注入依赖；沿用上层 conftest 的输出路径与资源作用域，不另建一套运行上下文。
- 验证进程、stdout、stderr 等独立资源均得到释放；测试兜底前先检查被测装具的清理结果。
- 中断与普通异常都必须保留原始对象；清理失败不能掩盖启动失败，也不能跳过其余资源。
- 产物镜像当前测试路径写入 `out/tests/unit/harness/processes/<测试名>/`；历史结果保留在原路径，不混作新运行证据。
