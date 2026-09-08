# 目录用途

协调 rollout 历史的只读 snapshot、索引缓存和有界分页查询。

# 可修改内容

- 统一 reader 上的快照生命周期、缓存失效和游标绑定。
- 按 include 和稳定 locator 查询已提交 Turn 与目标 item。

# 不可修改内容

- 不得恢复 Trace/v1 fallback 或静默修复已提交索引。
- 不得在这里编写 LangChain/content/Turn DTO 的纯转换。

# 规范

- 一个请求使用同一个读快照；异常必须释放快照并传播。
- 纯 DTO 转换交给 mapping；不得因单点详情缺少未请求正文而全量补读。
