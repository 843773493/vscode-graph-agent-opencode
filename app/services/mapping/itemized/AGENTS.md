# 目录用途

`app/services/mapping/itemized/` 存放 v2 selection 到 LangChain/history DTO 的纯映射。

```text
itemized/
├── selection.py                 校验选择并解析选中的canonical item/request正文
├── projection.py                生成可审计的projection evidence
├── history.py                   从selection plan投影历史item
├── prefix_frames.py             投影canonical前缀与封存前缀
├── provider_request.py          canonical正文 → provider请求内容
├── provider_history.py          provider历史正文 → 可见文本与reasoning行
├── content_blocks.py            读取文本与reasoning content block
├── canonical_langchain.py       canonical item → LangChain消息与摘要来源组
├── langchain.py                 context plan → LangChain消息及贡献metadata
├── carrier_dedup.py             识别重复的tool/content carrier
├── message_reasoning_merge.py   按canonical坐标合并reasoning与正文
├── resource_activation.py       sealed resource ref → 运行面投影
├── thread_seed.py               Thread创建记录 → 冻结seed
└── history_turns/               历史Turn DTO投影，见子目录指南
```

按数据流查找：先在 `selection.py` 确认选择和ref，再由对应投影模块转换；Provider请求与history DTO分别沿 `provider_request.py` 和 `provider_history.py`，Turn级内容、工具和详情由 `history_turns/` 组装。映射层不重新读取storage或补造canonical顺序。

# 可修改内容

- 可以实现 CanonicalItem、ContextSelectionEntry 和 request-only contribution 的无 I/O projection。
- 可以记录 source identity、order 和 capability loss 到映射结果 metadata。

# 不可修改内容

- 不得扫描 RolloutStorage、JSONL、SQLite 或 detail store。
- 不得实现业务规则、provider wire bridge 或持久化。

# 规范

- 只接受 Saver 已提交的 plan/snapshot，按 `selection.plan_ordinal` 消费，不自行排序。
- 缺失 ref、hash/length 不匹配和未知 payload 必须抛出，不得静默丢失。
