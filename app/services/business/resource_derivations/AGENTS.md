# 目录用途

维护已接受来源的业务语义派生，包括 AGENTS 原文与有序链、Skill frontmatter、metadata、activation、有效 catalog 和 Team ledger 的 membership/tasks。

## 职责树与定位

```text
resource_derivations/
├── agents.py             AGENTS 原文 facet 与登记顺序固定的指令链
├── skill_frontmatter.py  SKILL.md 安全标量文法与错误定位
├── skills.py             Skill metadata/activation 与覆盖优先级 catalog
└── team.py               冻结 collaboration ledger 的 membership/tasks facet
```

按要改变的事实选择文件：调整 Markdown frontmatter 语法进入 `skill_frontmatter.py`；改变 Skill 字段、activation 正文或 inline/gateway/workspace 覆盖规则进入 `skills.py`；改变 AGENTS 原文保真或有序拼接进入 `agents.py`；改变 Team ledger 的成员可见性或任务 facet 进入 `team.py`。新增来源类型应落到有具体领域名的 loader 文件，不能把解析器、来源读取和 registry 装配塞进通用模块。

# 可修改内容

- 可以维护纯业务 parser 和代码内版本化 loader。
- 可以维护 facet payload、固定覆盖优先级及对应测试。

# 不可修改内容

- 不读文件、目录、网络或当前配置，不建立来源或语义 registry。
- 不管理 watcher、运行时 task、Session 状态或 ContextStore writer。

# 规范

- loader 只消费图传入的不可变 accepted revision/payload。
- 无效输入返回明确 unavailable；程序缺陷直接抛出。
- 语义 revision 由既有 graph 根据 payload 决定，忽略字段不得进入 payload。
