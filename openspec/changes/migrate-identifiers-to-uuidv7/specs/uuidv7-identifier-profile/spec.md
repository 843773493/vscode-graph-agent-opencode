## Purpose

为项目所有 canonical 标识符（`session_id`、`thread_id` 及其它由唯一 id 工厂生成的持久身份）规定**唯一**的位 profile：**UUIDv7（RFC 9562）**。本 capability 是 id **生成位 profile** 的唯一 owner：它规定 id 由哪个显式直接依赖生成、如何 fail-closed、同毫秒内的排序保证、`sessions/YYYY/MM/DD` 日期桶如何从 id 内嵌时间戳推导与复核、时钟回拨行为、校验层命名、JS/浏览器侧的可用性边界，以及存量 v4 身份的一次性显式迁移。它**不**定义 VRN、ResourceIdentity、scope 闭集或拒绝码——那些归 `add-unified-virtual-resource-addressing` 等 change，本 capability 只具名引用。

## ADDED Requirements

### Requirement: canonical 标识符的位 profile 必须是 UUIDv7

所有 canonical 标识符（`ses_{32}`、`thr_{32}` 及其它由唯一 id 工厂生成的持久身份）的 32 位 hex payload MUST 满足 **UUIDv7（RFC 9562）** 位 profile：version 位 MUST 为 `7`，variant 位 MUST 属于 `8|9|a|b`；id MUST 保持既有 36-byte ASCII 外形（4 字节前缀 + 32 位小写 hex）。系统 MUST NOT 产出或接受 UUIDv4 位 profile 作为 canonical 身份。

#### Scenario: 工厂产出 v7 位 profile
- **WHEN** 唯一 id 工厂生成一个 canonical 标识符
- **THEN** 其 payload 第 13 个 hex MUST 为 `7`、第 17 个 hex MUST 属于 `8|9|a|b`，且总长 MUST 为 36 个 ASCII byte

#### Scenario: 拒绝非 v7 位 profile
- **WHEN** 任意 API、Proto、catalog 或 typed ref 提交 payload 第 13 个 hex 不为 `7` 的 canonical 标识符
- **THEN** 系统 MUST 在落盘前显式拒绝，MUST NOT 清洗、截断或以 hash 替代该 id

### Requirement: 唯一 id 工厂与其生成来源必须显式且 fail-closed

canonical 标识符 MUST 由唯一 id 工厂（`app/core/identifier.py` 的 `create_uuid_hex` / `create_prefixed_id`）生成；生产代码 MUST NOT 绕过该工厂直接调用底层 uuid 生成 canonical 身份。

生成来源 MUST 是一个**显式直接依赖**：`pyproject.toml` MUST 直接声明 `uuid-utils`（锁定版本 `>=0.16`），MUST NOT 依赖它作为 langchain-core/langsmith 的**传递依赖**偶然存在。依赖缺失、导入失败或 API 不可用时 MUST **fail-closed**（抛出详细错误），MUST NOT 静默回退到 UUIDv4 或任何虚假默认值。

#### Scenario: 直接依赖缺失即失败
- **WHEN** 运行环境未安装 `uuid-utils`（例如依赖被移除）
- **THEN** 工厂在首次生成时 MUST 抛出详细错误并停止，MUST NOT 回退到 `uuid.uuid4()`

#### Scenario: 生产不得绕过唯一工厂
- **WHEN** 生产代码需要生成 canonical session/thread 身份
- **THEN** 它 MUST 经唯一 id 工厂，MUST NOT 自行拼接 uuid 生成逻辑或另建第二个 canonical 生成入口

### Requirement: 同进程内同毫秒的 id 必须非递减且唯一

v7 的时间有序是本 capability 的核心价值，故 MUST 给出可验证的排序保证：

- **同进程**内、**同一毫秒**（相同 48 bit 时间前缀）生成的 id MUST **非递减**（按 32 位 hex 字典序）且 **MUST 唯一**，由 `rand_a` / 计数器方案提供；
- **跨进程或跨重启**只保证 **48 bit 毫秒分辨率**的时间序；MUST NOT 声称跨进程在同一毫秒内有序；
- 生成路径 MUST NOT 传入显式时间戳参数（实测传显式时间戳会破坏同毫秒内单调）；若允许注入时间源（如测试），该注入 MUST 保持与生产相同的单调合同。

#### Scenario: 同毫秒批量生成保持非递减且唯一
- **WHEN** 同一进程在同一毫秒内连续生成 20000 个 id
- **THEN** 按 32 位 hex 排序后与生成顺序逐字节一致，且集合去重后数量等于 20000

#### Scenario: 跨毫秒自然单调
- **WHEN** 同一进程跨越多个毫秒连续生成 200000 个 id
- **THEN** 全局按 32 位 hex 排序后与生成顺序一致，且全部唯一

### Requirement: sessions/YYYY/MM/DD 日期桶必须可由 id 内嵌时间戳推导且一致

`sessions/YYYY/MM/DD/{session_id}` 的日期段 MUST 与 `session_id` 内嵌的 48 bit Unix 毫秒时间戳按 **UTC** 推导出的日期逐段一致。系统 MUST 能仅凭 id 复核分桶日期，MUST NOT 只依赖另存的 `created_at` 作为唯一依据。

日期 MUST 使用 **UTC**（与既有分桶一致：`session_catalog_store.py` 以 `created_at.astimezone(UTC).date()` 生成 locator）；MUST NOT 改为本地时区。

#### Scenario: 分桶日期与 id 内嵌时间戳一致
- **WHEN** 校验一个 `sessions/YYYY/MM/DD/{session_id}` locator
- **THEN** 从 `session_id` 提取 48 bit 毫秒时间戳并按 UTC 求日期，其结果 MUST 与 locator 的 `YYYY/MM/DD` 完全一致

#### Scenario: 分桶与 id 不一致即 fail-closed
- **WHEN** 某 locator 的 `YYYY/MM/DD` 与 id 内嵌时间戳推导出的 UTC 日期不一致
- **THEN** 系统 MUST 报显式完整性错误，MUST NOT 静默接受、MUST NOT 扫盘修正、MUST NOT 悄悄改桶

### Requirement: 时钟回拨行为必须显式定义为进程内非递减钳制

系统时钟回拨（NTP 校时）时，同一进程内生成的 id MUST 保持非递减：MUST 使用进程内上一次已发放的时间戳作下界钳制（单调时钟保护），MUST NOT 因回拨而产出比此前更小的 id。跨进程/跨重启对回拨不做跨进程保证，但 MUST NOT 产出与既有 id 重复的值。

#### Scenario: 回拨不产生更小 id
- **WHEN** 系统时钟被回拨到早于上一次生成时刻
- **THEN** 新生成的 id 按 hex 序 MUST 不小于上一次生成的 id

### Requirement: 校验层命名必须只反映当前 profile 且不得双轨

把 `v4` 写进名字的标识、注释与 docstring（`_validate_uuid_v4_payload`、`_UUID_VERSION_HEX_INDEX`、「UUIDv4 位 profile」等）MUST 一并正名为与当前单一 profile 一致的名词（例如 `_validate_uuid_payload` / `_UUID_VERSION_HEX_INDEX` 语义变为「要求 version == 7」）。系统 MUST NOT 保留第二套并行校验函数或同概念异名；MUST NOT 长期同时接受 `v4|v7` 两种位 profile。

#### Scenario: 单一校验函数
- **WHEN** 任意入口校验 canonical 身份
- **THEN** 它复用同一个校验函数与同一组 version/variant 常量，MUST NOT 存在按 v4 与 v7 分支的第二套实现

#### Scenario: 命名不再误导
- **WHEN** 阅读校验层标识与 docstring
- **THEN** 其中不出现会误导为「当前仍接受 v4」的名称或描述

### Requirement: 存量 UUIDv4 身份必须一次性显式迁移且不得双轨

存量 UUIDv4 身份（`.boxteam/sessions/` 下的 session/thread id、SQLite 主键与相关持久记录）MUST 通过**一次性显式迁移**重编号为 UUIDv7；迁移 MUST 在受维护窗口与一致性备份约束下进行，MUST 使用可恢复账本记录进度，MUST 为每个被重编号的身份保留 source→target 的 lineage。

迁移 MUST NOT 双读、MUST NOT 扫盘重建、MUST NOT 提供旧 ID path alias；无法可靠归属或校验的形态 MUST fail-closed 或隔离到既有隔离惯例（`.boxteam/orphaned/`），MUST NOT 静默吸收。迁移窗口结束后，校验器 MUST 只接受 v7。

#### Scenario: 迁移保留 lineage 且不双读
- **WHEN** 迁移把某个 session 的 v4 身份重编号为 v7
- **THEN** 账本记录 source v4 → target v7 的映射，且运行路径只读取新身份，MUST NOT 同时按 v4 与 v7 双读

#### Scenario: 无法归属的形态 fail-closed
- **WHEN** 迁移遇到无法可靠归属或校验的旧身份/目录
- **THEN** 它 MUST 明确报错或隔离并保留原数据，MUST NOT 猜测目标、MUST NOT 扫盘补洞

### Requirement: SQLite 主键与索引不因 v7 重建

把 id 生成改为 v7 后，既有以 id 文本作 `TEXT PRIMARY KEY` 的表（`nodes.node_id`、`thread_catalog.thread_id` 等）MUST NOT 因 v7 而重建表或重建索引：v7 与 v4 的文本长度、类型与比较语义完全相同，改变的只是值的分布（获得时间局部性）。系统 MUST NOT 引入新列、新索引或迁移 DDL 来「支持 v7」。

#### Scenario: 无需 DDL 变更
- **WHEN** 实施 v7 切换
- **THEN** 既有主键表结构 MUST 保持不变，MUST NOT 出现仅为 v7 新增的表、列或索引

### Requirement: JS 服务进程与浏览器前端的 v7 可用性边界必须显式声明

系统 MUST 显式声明并区分各运行面的 v7 可用性，MUST NOT 伪造统一：

- `src/workspace-services/browser/server/` 与 `src/workspace-services/terminal/server/` 的后端进程由 **Node** 启动（`BOXTEAM_NODE_BIN`，见 `app/gateway/runtime/process.py`）；实测 Node 22 无 `crypto.randomUUIDv7`。其另行生成的 id（`term_` / `browser_` / `screenshot_` / `download_` / `page_` / `preset_` 等）MUST 被声明为**非 canonical 身份**，允许继续使用 v4。
- `src/clients/web` 是浏览器构建产物，**没有** `Bun.*`（实测 `src/clients/**` 无任何 `Bun.` 引用）；其生成的附件 file id（`inline:{uuid}:{name}`）MUST 被声明为**非 canonical 身份**，允许继续使用 v4。
- 上述非 canonical id MUST NOT 被当作 session/thread 身份，MUST NOT 进入 canonical 校验器所在的命名空间。

#### Scenario: 非 canonical id 明确豁免
- **WHEN** Node 服务进程或浏览器前端生成一个本地 id
- **THEN** 该 id MUST 被标注为非 canonical 身份，MUST NOT 断言其已满足 v7 profile，也 MUST NOT 让 canonical 校验器接受它作为 session/thread 身份

#### Scenario: 前端不得假装可用 Bun
- **WHEN** `src/clients/web` 需要生成 id
- **THEN** 它 MUST NOT 依赖 `Bun.randomUUIDv7`（浏览器无 `Bun`），并 MUST 按上述非 canonical 边界处理

### Requirement: 与 VRN / ResourceIdentity 的边界必须具名引用

本 capability 只拥有 id 的**生成位 profile**；MUST NOT 定义或改写 VRN 语法、scope 闭集、`scope_id` 语义、`kind` 闭集、拒绝码或 ResourceIdentity。上述内容 MUST 具名引用对应 change，本 capability MUST NOT 复制其定义。

#### Scenario: 生成位 profile 与寻址身份不可混用
- **WHEN** 某处需要引用资源寻址或资源身份
- **THEN** 它 MUST 具名引用 `add-unified-virtual-resource-addressing` / `migrate-session-context-uri-to-vrn` 等 owner，MUST NOT 借本 capability 的 id profile 条款表达寻址语义

