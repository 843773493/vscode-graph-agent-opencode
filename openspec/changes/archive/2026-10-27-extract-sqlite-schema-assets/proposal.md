## Why

SQLite 的建表、索引、触发器与迁移 SQL 分散在 Python 字符串中，结构审查和版本核对需要穿过执行代码。将静态数据库定义提取为 SQL 资产，使结构与执行协调各有唯一来源。

## What Changes

- 将应用自有 SQLite 的静态 DDL 和迁移语句移入各存储 owner 的 `.sql` 文件，删除对应内联文本。
- Python 保留连接、事务、版本顺序、条件判断、动态参数及必要的数据转换协调，不另建迁移引擎。
- 核对源码与发行包均可加载 SQL 资产，缺失或损坏时明确失败。
- 保持 schema 版本、迁移顺序、表结构、数据与现有外部行为；不趁本次重构新增迁移或兼容旧格式。

## Capabilities

### New Capabilities

无。本变更是实现重构，`skip_specs: true`，不新增产品合同。

### Modified Capabilities

无。已有 Session、Gateway、rollout 和工作区存储合同继续适用。

## Impact

涉及 `app/core/` Session catalog/control 与 SQLite 基础设施、Gateway 状态/联邦库、rollout/activation、附件、导航队列与工作区状态/文件操作库，以及发行包资源收集。测试中的专用装具 DDL、第三方库及 `reference_repo/` 不迁移；普通业务查询不作无关重写。
