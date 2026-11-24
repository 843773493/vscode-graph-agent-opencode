## Why

已有失败原因在传输、实时事件、历史查询和前端中被丢失或改写，另一些入口又把长诊断重复铺在页面与通知中。需要贯通原 owner 的失败事实，让默认界面简短、详情可查，避免逐异常编写文案或另建错误系统。

## What Changes

- 复用公共 `Error(code, message, details)`、SDK 提取及各原协议 adapter，保留真实原因、状态和已有作用域／请求关联；未知异常不要求专用文案。
- 业务 owner 决定结果及恢复资格，客户端纯展示派生摘要，Workbench 原入口负责通知和输出；职责沿已有模块落实。
- 默认摘要与有界公开原因分开，同一操作只选择一个主要提示出口。字段校验、批量部分成功和结果未知保留原语义；字段定位及文档时效由原编辑器负责。
- 统一到既有输出命令及日志 query／locate，迁齐设置页和插件详情的日志查看入口，删除重复读取／展示及反向依赖。按原失败来源和已知连接定位，能力不足时如实只打开通道。
- HTTP 正文或来源解析失败保留已收到的响应事实；共享 realtime 区分连接与单订阅故障，不由客户端猜业务终态。
- 聊天 failure 随原 rollout convergence 终态提交，消息流、恢复及历史消费同一事实；attempt 失败不替代成功 Turn，不新增恢复服务或错误存储。
- 公开错误与本链路生成的诊断在原出口脱敏并限量，验证真实入口的信息保真、提示归属、导航及交互预算。文案仅来自原原因、少量通用提示和原领域已有状态。

## Capabilities

### New Capabilities

- `failure-reporting`: 错误交付、诊断关联、保护与按 owner 的呈现规则。

### Modified Capabilities

- `session-turn-history`: 历史保留原执行失败，缺失原因如实披露，不扫描日志补齐。
- `turn-message-stream`: 实时、恢复快照及历史共享终态原因，保持原控制语义。

## Impact

- Python／Node SDK failures、`app/core/failures.py`、原 HTTP／Gateway／realtime／DAP 边界，以及原 DTO／协议生成消费者。
- 原执行收尾、rollout／message stream、Job 恢复、历史 reader 与客户端状态投影。
- 原插件／MCP／配置／主题／资源及维护入口，Web／Electron 共用 Workbench、React 和输出导航。
- 沿原 API、权限、取消、重试及释放规则实施；不新增端口、常驻连接、轮询、错误数据库、文案注册表或配置开关，不自动修改用户 Home／数据。
