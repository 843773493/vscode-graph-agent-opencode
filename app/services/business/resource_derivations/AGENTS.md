# 目录用途

维护已接受来源的业务语义派生，包括 Skill frontmatter、metadata、activation 与有效 catalog。

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
