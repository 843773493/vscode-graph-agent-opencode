## ADDED Requirements

### Requirement: LLM 请求日志有界搜索与原记录定位

工作区搜索 SHALL 由原请求日志 owner 按 catalog 中的 Session 有界查询已保留的 request、response、upstream 正文。请求记录保持现有 Session 归属，不推断 Thread，不作为聊天消息重复返回。正文采用固定字段顺序、两空格缩进、保留 Unicode 的 JSON 文本，匹配为区分大小写的非重叠字面量；位置采用从零开始、右端不包含的 Unicode 码点区间。搜索不新增正文副本或持久索引，不改变原日志留存规则。

每页 SHALL 至多检查 64 个目录节点、128 条记录、读取 8 MiB 正文，正文调度预算为一秒，并服从请求剩余总期限。单条记录硬上限为 1 MiB；单个日志目录至多枚举 512 个 JSON 文件。达到预算 SHALL 披露扫描量、受限原因和可用续点；无法推进的单项或目录超限 SHALL 不提供循环 cursor。一次同步读取不能被强行中断，取消后必须等待 worker 结算再释放 Session 读准入。

续页 SHALL 绑定 Gateway、工作区、原查询、catalog revision、当前 Session 日志目录版本及正文位置，目录增删或当前记录变更时明确拒绝。各 Session 是其实际读取时的快照，已完成 Session 新增日志需重新查询。

命中 SHALL 使用原 Session 资源 URI，并独立携带 Session ID、日志文件名、正文版本和位置。精确读取 SHALL 在原 Session 读准入内只读取目标记录，核验归属、版本及位置后返回原详情与搜索文本；删除、留存裁剪或改写 SHALL 返回命中失效，不退回最新记录或 Session 顶部。客户端沿原请求详情展示临时原文并高亮，关闭或切换定位即释放，不拉全量日志或写入聊天历史。

#### Scenario: 请求独有正文命中

- **WHEN** 用户明确选择 LLM 请求类别，查询未打开 Session 中只存在于模型请求正文的词
- **THEN** 原 owner 返回精确 Session／记录／位置，打开后只加载该记录，展示原请求详情及命中原文，不查询未选类别

#### Scenario: 旧记录和续点失效

- **WHEN** 原记录已被改写、删除或裁剪，或续页的目录版本已变化
- **THEN** owner 明确拒绝旧引用或 cursor，不改投另一条记录；普通原请求日志列表继续沿同一解码实现读取

### Requirement: 请求日志查询拒绝保留机器原因

原日志 owner SHALL 统一发布续点与记录引用错误，携带 `owner=llm_request_log`、`code`、`reason` 和原详细消息。格式／结构错误、归属／查询不符及越界续点 SHALL 返回 HTTP 400 `invalid_request_log_cursor`；目录或正文版本变化 SHALL 返回 HTTP 409 `stale_request_log_cursor`。精确定位已删除、变化、越界或超限的原记录 SHALL 保留 HTTP 409 `stale_request_log_reference`。原读取权限、取消和内部错误不改标为续点失效。

原因 SHALL 由实际校验点产生，区分 `malformed`、`owner_mismatch`、`query_mismatch`、`position_out_of_bounds`、`catalog_changed`、`directory_deleted`、`directory_changed`、`record_deleted`、`record_changed` 和 `record_size_limit` 的适用情况。Gateway SHALL 沿原分支传递这些字段、真实 HTTP 状态及原输入 cursor，零页交付时不推进续点，不解析消息文字推断原因。

#### Scenario: 无效输入与过期版本可区分

- **WHEN** 同一请求日志搜索分别使用损坏续点、另一查询的续点或已变化日志的续点
- **THEN** 原 API 与联邦分支返回对应机器代码和原因，保留本次 request_id、原详细消息及输入续点，不报告完整零匹配
