# 目录用途

`turn_history/` 实现会话节点内的 Trace 持久化、按 Job Turn 组织的读取模型及恢复边界。

## 可修改内容

- 可以修改 Turn manifest、operation generation、timeline、cursor 和投影读取实现。
- 可以增加针对崩溃恢复、分页稳定性和会话路径边界的聚焦模块。

## 不可修改内容

- 不要在这里编排 Job、迁移后台任务或前端展示流程。
- 不要绕过统一会话路径解析器拼接固定 sessions 路径。
- 不要用 Turn 派生数据反向覆盖 checkpoint、消息历史或会话目录索引。

## 规范

- Trace 正文唯一存于会话节点 `logs/traces/trace.sqlite`，`trace_database.py` 负责事务与结构化索引，SQL 迁移位于 `migrations/`。生成列及索引从正文自动维护，不另写 JSONL、消息副本或 compact 索引。
- `trace_page.py` 负责有界分页和事件解析，`trace_stream.py` 分批补读与完整恢复解析，`trace_search.py` 保留 JSON 展示文本的字面匹配与预算。普通查询只消费命中事件及目标 Job 的 owner 事实；启动恢复必须耗尽完整快照，所有校验通过后才能发布恢复结果。
- 损坏、越界或不一致必须快速失败，禁止扫描磁盘后猜测修复。
- 单个实现文件尽量控制在 400 行以内。
- 搜索续页绑定 SQLite 提交版本；追加或改写后明确失效。传输游标绑定事件逻辑字节边界和采样摘要，搜索引用校验完整正文 SHA256；正文只存一份。
- 不为开发中间 JSONL 自动导入、修复或双读；存在旧数据而无 SQLite 时明确报错，保留原件。
