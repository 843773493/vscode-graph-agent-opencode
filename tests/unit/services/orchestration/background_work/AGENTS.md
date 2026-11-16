# 目录用途

镜像 app/services/orchestration/background_work/，验证后台工具任务和持久邮箱的流程行为。

## 可修改内容

- test_registry.py：后台任务终态、启动恢复、外部效果对账和 handler checkpoint。
- test_mailbox.py：精确 Thread 消息、消费 checkpoint、确认及重启重放。

## 不可修改内容

- 不复制生产调度器、存储逻辑或领域值对象。
- 不放宽消息身份、提交顺序、冲突、恢复和错误断言。

## 规范

- 依赖经 pytest fixture 注入；会话 bundle 与控制库使用隔离工作区。
- 真实进程崩溃和重启场景留在 tests/integration/backend/sessions/test_background_work_recovery.py。
- 运行输出镜像本目录下的测试文件写入 out/tests/；历史产物保留原位置。
