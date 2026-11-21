# 目录用途

Node SDK 的真实 HTTP、子进程、Worker I/O 与关闭验收。

## 可修改内容

经公开 API 组合运行的测试和专用 Worker 输入。

## 不可修改内容

不以 Bun 运行替代声明使用原生 Node 的场景，不修改 SDK 生产代码适配测试。

## 规范

原生 Node 与 Bun 驱动各有矩阵入口；Node 子进程仍使用目标 Node。可变产物由 TestRunContext 从当前测试路径派生。
