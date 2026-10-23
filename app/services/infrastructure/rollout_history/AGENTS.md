# 目录用途

协调 rollout 历史的只读 snapshot、索引缓存和有界分页查询。

## 职责树与依赖方向

```text
rollout_history/
├── snapshot.py  IndexedHistorySnapshots、已提交 snapshot 与索引视图缓存
├── cursor.py    HistoryCursorBinding、请求绑定、游标校验和编解码
└── paging.py    HistoryPageReadMixin、基于单一 snapshot 的有界读取
```

`RolloutHistoryReader` 继承 `paging.py` 的 `HistoryPageReadMixin`；该 mixin 继承
`cursor.py` 的 `HistoryCursorMixin`。分页模块读取 `snapshot.py` 提供的索引视图；游标
模块用同一视图的 revision 和 upper bound 绑定 continuation cursor。保留该方向，避免
snapshot 缓存依赖分页实现，或在分页代码中重新实现 cursor 合同。

# 可修改内容

- 统一 reader 上的快照生命周期、缓存失效和游标绑定。
- 按 include 和稳定 locator 查询已提交 Turn 与目标 item。

# 不可修改内容

- 不得恢复 Trace/v1 fallback 或静默修复已提交索引。
- 不得在这里编写 LangChain/content/Turn DTO 的纯转换。

# 规范

- 一个请求使用同一个读快照；异常必须释放快照并传播。
- 纯 DTO 转换交给 mapping；不得因单点详情缺少未请求正文而全量补读。
