# 目录用途

镜像 app/services/orchestration/background_work/，验证后台工具任务和持久邮箱的流程行为。

## 可修改内容

- test_registry.py：后台任务终态、启动恢复、外部效果对账、派生请求有界调度／按 Thread 恢复／发布取消裁决和 handler checkpoint。
- test_mailbox.py：精确 Thread 消息、消费 checkpoint、确认及重启重放。

## 不可修改内容

- 不复制生产调度器、存储逻辑或领域值对象。
- 不放宽消息身份、提交顺序、冲突、恢复和错误断言。

## 规范

- 依赖经 pytest fixture 注入；会话 bundle 与控制库使用隔离工作区。
- 真实进程崩溃和重启场景留在 tests/integration/backend/sessions/test_background_work_recovery.py。
- 运行输出镜像本目录下的测试文件写入 out/tests/；历史产物保留原位置。
- 派生结果替身只返回已提交结果的引用；不得以普通消息或虚构候选正文绕过完成顺序。队满须核持久任务和本地 Task 均未增加，恢复须核上游调用数为零。
