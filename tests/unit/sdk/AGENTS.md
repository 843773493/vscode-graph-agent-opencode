# 目录用途

共享 SDK 的独立单元测试，按 Python／Node 运行时组织。

## 可修改内容

SDK 单模块的公开 API、状态转换与受控异步行为。

## 不可修改内容

不放产品 Job／Thread 接入、真实 HTTP、子进程或 Worker 组合验收。

## 规范

日常测试使用正常的 editable／workspace 安装；不逐用例打包下载。产品消费者测试留在对应业务目录。
