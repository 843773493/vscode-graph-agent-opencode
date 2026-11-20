# 目录用途

验证 Node SDK 的公开运行时合同及真实线程资源收尾。

## 可修改内容

- Node SDK 独立包的测试脚本与专用 Worker 输入。

## 不可修改内容

- 不改 `sdk/node` 产品实现、包声明、根依赖锁或插件业务代码。
- 不通过 mock/fake 代替本测试声明的 worker_threads 与文件 I/O。

## 规范

- 测试从 `@boxteam/plugin-sdk/maintenance` 公开入口导入 `startMaintenance`。
- 运行需由进程外超时和内存保护包围；产物写入 `out/tests/sdk/node/`。
