# 目录用途

`migration/` 只承载一次性 `legacy_import_v1_to_v2` 的 staging、report、quarantine 和 rollback audit。

## 职责树

```text
migration/
├── importer.py       一次性导入、审计状态和恢复入口
├── source.py         严格只读扫描 v1 manifest 与正文，生成候选报告
├── artifacts.py      普通文件校验、哈希清单、原件快照、审计和原子安装
├── manifest.py       v1 正文校验和及 final pointer 读写
├── legacy_adapter.py v1 envelope 校验，映射 legacy candidate
├── semantics.py      来源身份、终态证据、可转换性和损失分类
├── builder.py        将已接受候选写入 v2 item、Turn 和投影
├── staging.py        绑定临时 rollout 路径并用现行 reader 验收
└── quarantine.py     为拒绝项写入受保护的审计引用
```

```text
importer
├── source
│   ├── artifacts             安全读取原件并计算快照清单
│   ├── manifest              校验 v1 正文引用及显式 final pointer
│   ├── legacy_adapter        校验 v1 envelope
│   └── semantics             生成候选审计和终态证据
├── builder
│   ├── legacy_adapter       转换单个 legacy candidate
│   └── semantics            判定终态证据及转换损失
├── staging                  写入并验收隔离的 v2 rollout
├── quarantine              保存拒绝原因与受保护原件引用
└── artifacts               验证源和空目标后原子发布已验收 staging
```

新增逻辑按数据经过的阶段放入对应 owner：v1 文件读取归 `source.py`，普通文件与耐久性归 `artifacts.py`，候选语义归 `semantics.py`，v1 到 v2 的映射归 `legacy_adapter.py`，v2 写入和最终验收分别归 `builder.py`、`staging.py`。只有协调这些阶段的状态流转属于 `importer.py`。

# 可修改内容

- 可以只读解析 v1 artifact、生成 v2 migration plan 和审计报告。
- 可以调用 v2 writer 完成显式 import 安装。

# 不可修改内容

- 不得被正常 history/provider/checkpoint/runtime/context compiler 调用。
- 不得提供长期 compatibility API、dual writer 或 v1 fallback。

# 规范

- v1 source 只读、失败可审计；未知角色、版本、identity 或 hash 必须 quarantine/报错。
