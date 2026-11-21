# 目录用途

Node SDK 的维护、观察和日志单元测试。

## 可修改内容

公开包入口的状态与异步行为；按测试原本的 Bun／Node 引擎运行。

## 不可修改内容

不在此执行真实 HTTP、子进程或 Worker 文件操作。

## 规范

矩阵分别提供 unit-sdk-node-bun 与 unit-sdk-node-native；必须带进程外期限和内存保护。
