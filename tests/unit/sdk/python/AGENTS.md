# 目录用途

Python SDK 的生命周期、维护、日志与请求上下文单测。

## 可修改内容

经 boxteam_plugin_sdk 公开入口验证独立行为及本目录 fixture。

## 不可修改内容

不导入 app 产品实现，不启动完整 Backend 或插件进程。

## 规范

pytest 自动取得 unit 标记；共享 logger 隔离复用 tests/support/runtime_logging.py。测试临时数据使用原 tmp_path 与镜像输出路径。
