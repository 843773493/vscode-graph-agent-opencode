# 目录用途

`runtime/` 负责实时 context ledger、detail store、composer、reconciliation 和 stream accumulator 的运行时适配。

职责沿数据流分层，按“实时来源与流输入 → 请求编译 → 持久化 detail”查找：

```text
runtime/
├── context_sources/       来源 identity、revision 与待发送 delta 的内存协调
├── stream_accumulator.py  Provider stream → 当前 execution 的 item draft
├── ledger.py              实时 contribution 记录与稳定排序
├── reconciliation.py      history view 与 source overlay 的 revision 对账
├── prefix_compiler.py     canonical facts → Provider 请求前缀
├── composer.py            ContextRequestPlan 组装与 request hash
├── summary_compiler.py    独立摘要候选请求
└── detail_*               受保护 detail 的 manifest、envelope、文件与保留期
    ├── detail_manifest.py typed identity、locator 与登记记录校验
    ├── detail_payload.py  v3 envelope 编码、解析与 hash/length 校验
    ├── detail_files.py    物理路径、非跟随读取与排他发布
    ├── detail_store.py    detail 唯一写入/读取 owner
    └── detail_removal.py  已批准 detail 的删除与 retention GC
```

`context_sources/` 不写持久状态；`stream_accumulator.py` 只产生内存 draft；`detail_store.py` 是唯一 detail I/O 入口。请求前缀、摘要请求和 history reconciliation 各守自己的边界，不互相维护第二份状态。

# 可修改内容

- 可以维护内存 draft、source provenance、cache-preserving overlay 和受保护 detail port。
- 可以调用 Saver 提供的提交快照接口。

# 不可修改内容

- 不得把内存 view 当作 canonical JSONL/SQLite 事实源。
- 不得直接读取 v1、LangChain history 或 Provider wire。

# 规范

- source revision、hash、length、ordinal 和 loss 必须显式记录；失败不得静默降级。
