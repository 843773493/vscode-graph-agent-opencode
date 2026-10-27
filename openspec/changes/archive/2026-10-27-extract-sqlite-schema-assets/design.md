## Context

动机见 proposal。当前应用自有 SQLite 使用两类执行方式：`SQLiteStateDatabase` 的有序字符串/回调迁移，以及 rollout 等 owner 的连接内显式执行。SQL 提取必须适配现有事务边界，不能把逐条 execute 换成会隐式提交的 executescript。

## Goals / Non-Goals

**Goals:** 每份静态结构与迁移 SQL 只有 `.sql` 一个来源；Python 继续决定何时、以何种事务和参数执行。源码运行及发行包均加载同一资产。

**Non-Goals:** 不统一各业务数据库，不改变版本、存储布局和恢复策略，不迁移普通 CRUD，不新增数据库框架或旧格式兼容。

## Decisions

1. SQL 归所属存储 owner，就近命名；多版本迁移放该 owner 的 `sql/`，按现有版本编号。Python 显式列出版本与文件，不扫描目录推导迁移顺序，也不因新文件自动执行。
2. 复用标准库 `importlib.resources` 读取随包分发的 UTF-8 SQL，入口位于现有 SQLite 基础设施；不依赖 cwd 或向上推导源码根，不保留内联 fallback。文件缺失、不可读或 SQL 执行失败直接沿现有错误链暴露。
3. 提取时保留 SQL 文本和执行方法。字符串迁移仍交给既有 runner；回调中需要条件、行转换或应用函数的部分留在 Python，其静态 SQL 移出并继续参数绑定。动态标识符只保留必要受控替换，不引入通用模板引擎。
4. 每个数据库纵向迁齐声明、加载方、间接消费者、测试与发行资源后独立验证/提交。原迁移版本不加号、不重排；现有库重开不补跑迁移。
5. 范围盘点以 Python AST 中实际 SQL 常量及执行调用为准，文本搜索作候选；注释、文档和测试装具不计生产 DDL。静态结构、迁移 DML及伴随索引/触发器一并迁移，普通运行期查询留在原 owner。

## Risks / Trade-offs

- SQL 资产漏进发行包 → 检查真实资源收集入口，并从隔离发行目录加载/初始化数据库；不只断言文件存在。
- 提取导致隐式提交或迁移号漂移 → 保留原 execute/executescript 调用与版本表；验证失败回滚、冷重开和已存在数据。
- 动态 SQL 误提取为静态文本 → 逐 owner 审查格式化参数和条件迁移，保留绑定与受控标识符协调。
- 共享工作树并发修改 → 保留原始文本/源码指纹，限定私有 index 提交，不覆盖其它 owner 改动。

## Migration Plan

按 Gateway、Session catalog/control、rollout/activation、其余工作区 owner 分批实施。每批先保存原迁移文本及 schema 基线，再提取并验证新库、既有版本升级、冷重开和失败语义。结构及数据版本保持不变，代码回退只需连同 SQL 资源一起回退，不产生额外数据库降级步骤。
