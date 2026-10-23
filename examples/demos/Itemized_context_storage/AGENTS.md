# 目录用途

`Itemized_context_storage/` 是当前主仓库 item 化模型上下文的独立测试与教学 demo，展示 canonical item、JSONL、SQLite catalog/view、request-only 计划和 transcript 投影之间的边界。

```text
Itemized_context_storage/
├── README.md               demo 使用和存储模型说明
├── package.json            Bun 启动与测试命令
├── scripts/                不写入运行目录的构建检查
├── src/
│   ├── canonicalItems.js     canonical JSON/hash、示例 item 和紧凑内容投影
│   ├── canonicalJsonl.js     按 catalog 字节偏移读取并校验 canonical JSONL
│   ├── contextProjections.js request plan 与 transcript 的纯投影构造
│   ├── runtimePaths.js       runtime 路径解析与显式子树清理
│   ├── storage.js            协调 canonical JSONL、SQLite catalog/view 和投影
│   ├── cli.js                初始化 demo 并输出 runtime 快照
│   ├── server.js             提供只读 HTTP 展示入口
│   └── web/                  展示真实存储状态的浏览器界面
├── tests/                   验证 canonical item、request plan 和 transcript
└── runtime/                 默认演示运行时数据，Git 忽略
```

数据读写关系：

```text
canonicalItems.js ──> sample item
storage.js
├── 写入 canonical 正文 ──> rollout.jsonl
├── SQLite ──> index.sqlite
│   ├── item_catalog     身份、顺序、hash 和 JSONL locator
│   ├── item_projections 紧凑展示预览
│   └── context view     included/omitted 和顺序
└── canonicalJsonl.js <── catalog 偏移；读取 rollout.jsonl 并校验记录身份

storage.js ──> contextProjections.js
├── active view + included items ──> request-plan.json
└── user/assistant items ──> transcript.json

runtimePaths.js ──> 约束 runtime 子路径与 reset 目标
cli.js ──> 初始化并输出 runtime 快照
server.js ──> /api/state ──> web/ 页面展示同一 runtime 状态
```

JSONL 持有 canonical item 正文；SQLite 中的 catalog、preview 和 view 都可从 canonical items 派生，但初始化会先检查两者边界，拒绝吸收未登记 JSONL 尾部。

测试运行数据保留在仓库根目录的 `out/tests/examples/demos/Itemized_context_storage/tests/storage.test/workspace/<run_id>/runtime/`，不写入 demo 的 `runtime/`。

# 可修改内容

- demo 自己的 Bun 源码、网页、测试和说明
- `runtime/` 下由 demo 产生的运行时数据
- 测试通过显式 `runtimeBasePath` 将存储产物定向到独立测试输出目录
- demo 自己的 `package.json` 和锁文件

# 不可修改内容

- 不要导入或修改主仓库 `app/`、`src/` 的生产运行时
- demo 正常运行时将数据写到 `runtime/`；测试必须显式指定位于 `out/tests/` 的 runtime base
- 不要把 `Message` 或 transcript 投影当作 canonical item 的存储源

# 规范

- JavaScript 使用 ESM，通过 Bun 运行
- `runtime/` 是默认演示运行时根目录，清理操作只能作用于明确传入的 runtime base 子目录
- runtime 相对路径中的 `/` 和 `\` 都表示子目录分隔符，空路径段、`.`、`..` 与非法字符会被拒绝
- JSONL 保存 canonical item 正文；SQLite 只保存索引、投影和 view 状态
- `runtimePaths.js` 只负责 runtime 子路径约束和清理；`canonicalJsonl.js` 只负责 canonical 文件高水位及偏移读取；`storage.js` 协调 JSONL 与 SQLite
- 失败时暴露具体错误，不能静默降级
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
