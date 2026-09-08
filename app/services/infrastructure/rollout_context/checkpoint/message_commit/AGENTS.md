# 目录用途

准备 checkpoint 消息引用与有序 canonical group 的原子提交批次。

# 可修改内容

- 既有 group 的幂等性验证、消息/item 双坐标分配和不可变 JSONL 布局。

# 不可修改内容

- 不得自行提交事务、改写已提交 JSONL、生成 Provider/LangChain payload。
- 不得复制 domain schema 或返回伪造的空 group。

# 规范

- 只调用注入的 codec/read port；实际 fsync/catalog/commit 由 persistence 协调。
- 一个 group 的每个 item 都独立分配 sequence/locator；消息只引用 group anchor。
