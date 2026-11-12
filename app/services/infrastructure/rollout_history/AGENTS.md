# 目录用途

协调 rollout 历史的只读 snapshot、视图解析和有界分页查询。

## 职责树与依赖方向

```text
rollout_history/
├── snapshot.py  IndexedHistorySnapshots、已提交 snapshot 与当前事务的视图解析
├── cursor.py    HistoryCursorBinding、请求绑定、游标校验和编解码
├── paging.py    HistoryPageReadMixin、基于单一 snapshot 的有界读取
├── message_search.py  同一 snapshot 按明确类别查询消息／工具结果及核验位置
└── tool_result.py     复用历史调用身份与原 ToolOutputStore 的不可变完整输出
```

`RolloutHistoryReader` 继承 `paging.py` 的 `HistoryPageReadMixin`；该 mixin 继承
`cursor.py` 的 `HistoryCursorMixin`。分页模块读取 `snapshot.py` 提供的索引视图；游标
模块用同一视图的 revision 和 upper bound 绑定 continuation cursor。保留该方向，避免
snapshot 解析依赖分页实现，或在分页代码中重新实现 cursor 合同。

# 可修改内容

- 统一 reader 上的快照生命周期、视图解析和游标绑定。
- 按 include 和稳定 locator 查询已提交 Turn 与目标 item。

# 不可修改内容

- 不得恢复 Trace/v1 fallback 或静默修复已提交索引。
- 不得在这里编写 LangChain/content/Turn DTO 的纯转换。

# 规范

- 一个请求使用同一个读快照；异常必须释放快照并传播。
- 纯 DTO 转换交给 mapping；不得因单点详情缺少未请求正文而全量补读。
- 正文搜索按显式类别读取当前可见 canonical 用户／助手或工具结果 item，沿原正文 reader 和文本投影语义；完整工具输出只沿该 item 引用读取原 ToolOutputStore，不保存第二份搜索正文。SQL locator、正文读取和响应均受原查询预算约束。

工具结果目录与接受共用同一 snapshot 和可见载体选择；目录不读正文，接受通过独立 item 选择器核验 Turn 并补载原调用详情，不冒充正文搜索区间。
