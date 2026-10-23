# 目录用途

存放 itemized context demo 的存储核心、CLI 和本地 HTTP 服务。

```text
src/
├── canonicalItems.js       canonical JSON/hash、示例 item 和紧凑内容投影
├── canonicalJsonl.js       canonical 文件高水位校验和按 catalog 偏移读取
├── contextProjections.js   request plan 与 transcript 的纯投影构造
├── runtimePaths.js         runtime 路径约束与明确指定子树的清理
├── storage.js              协调 canonical JSONL、SQLite catalog/view 和投影；测试可指定 runtimeBasePath
├── cli.js                  demo 初始化与 runtime 快照输出
├── server.js               只读 HTTP 展示服务
└── web/                    展示真实存储状态的浏览器界面
```

# 可修改内容

- demo 存储模型和演示服务

# 不可修改内容

- 不要依赖主仓库生产模块
- demo 默认运行时状态只写到 `runtime/`；测试须显式指定 `out/tests/` 下的 runtimeBasePath

# 规范

- 使用 ESM 和 Bun 内置能力
- 存储边界要在代码中保持可读
- `runtimePaths.js` 不读取存储内容；`canonicalJsonl.js` 不查询 SQLite；两者由 `storage.js` 协调
- 模型 context、transcript 和 request-only 数据必须明确区分
- context plan 与 SQLite 投影中的内容长度统一表示 UTF-8 字节数；展示预览最多保留 120 个 Unicode 码点
- 初始化以 JSONL/SQLite canonical items 为源；先核对 JSONL 文件尾部与最后一条 catalog 记录的高水位，不吸收未登记字节；读取后核对 item_id 和 item_sequence，避免 catalog 偏移错指时返回错误记录；派生 view、request plan 或 transcript 任一步失败后，保留 canonical 数据并允许重试重建；成功后的同实例重复调用复用 store，并发调用共用一次执行，初始化期间拒绝 reset
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
