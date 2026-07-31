# 目录用途

`turn_history/` 实现会话节点内按完整 Job Turn 组织的派生读取模型及其崩溃恢复持久化。

## 可修改内容

- 可以修改 Turn manifest、operation generation、timeline、cursor 和投影读取实现。
- 可以增加针对崩溃恢复、分页稳定性和会话路径边界的聚焦模块。

## 不可修改内容

- 不要在这里编排 Job、迁移后台任务或前端展示流程。
- 不要绕过统一会话路径解析器拼接固定 sessions 路径。
- 不要用 Turn 派生数据反向覆盖 checkpoint、消息历史或会话目录索引。

## 规范

- `store.py` 只暴露协议实现并组合聚焦组件；文件格式、operation 应用和分页分别下沉。
- manifest 是 active operation generation 和 projection epoch 的权威来源。
- 损坏、越界或不一致必须快速失败，禁止扫描磁盘后猜测修复。
- 单个实现文件尽量控制在 400 行以内。
