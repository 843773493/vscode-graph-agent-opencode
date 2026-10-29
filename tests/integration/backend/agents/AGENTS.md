# 目录用途

存放 Agent、工具组、Skill 和 Node 调试服务之间的集成测试。

## 可修改内容

- 真实 Agent runtime 配合本地模型协议桩的测试。
- Agent 工厂、工具调用边界和真实 Node Inspector 的集成测试。

## 不可修改内容

- 只测试工具 factory 参数映射的单元测试。
- 使用真实外部模型验证自主行为的 E2E。

## 规范

- 明确区分“脚本模型驱动”与“真实模型驱动”。
- 断言必须覆盖真实后端结果、工具轨迹或工作区状态。
- 不能仅根据调用次数或预设顺序判定 Skill 加载成功。
- `test_chatgpt_auth_errors.py`经真实凭据owner、模型工厂、Agent服务和ASGI API核验读取／结构／迁移／刷新发布失败及同入口恢复；日志故障通过隔离目录中的真实OS写入拒绝注入。`integration-chatgpt-auth-errors`保持60秒/4GiB，原件、精确Provider归属、request_id和凭据不入日志均须核对，不代真实OAuth网络、Gateway、独立Backend或Web验收。
