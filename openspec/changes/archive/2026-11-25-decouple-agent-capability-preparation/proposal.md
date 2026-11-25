## Why

配置、Skill 来源、上下文、会话摘要和搜索已经有各自的查询 owner。当前缺口是工具目录仍经 AgentExecutionService 进入执行装配大工厂，借助合成 Session／Thread 取得定义，随后又补写 custom 目录。若再为前端、模型工具或内部模块分别增加查询实现，会重复来源读取、派生、分页和缓存。

本变更以收敛查询链路为主：同一领域、同一视图复用原业务读取入口；当前定义与冻结历史各沿原来源。正式执行的本地工具校验顺序及压缩检查未使用对象同步收窄，避免查询解耦后继续保留原大工厂的替代模式。

## What Changes

- 以查询复用为主线：配置／Agent、Skill 来源、上下文／封存历史、信息摘要、搜索和运行状态继续使用各自原业务 reader。当前工具管理与模型内冻结工具发现区分视图，共享声明／查询规则，不混用最新与历史快照；已有联邦搜索与资源聚合继续原位使用，不新建查询服务或聚合总入口。
- 将工具目录读取收回现有 AgentToolCatalog，查询与执行共享权威参数声明、schema 导出和必要的工具构造逻辑；删除 custom 定义补写、执行服务目录缓存、tool_catalog 模式与合成身份，保留 custom 原来源／分组、配置描述覆盖及策略匹配。
- 复用已有公开 schema exporter、工具策略／依赖检查和无模型中间件工厂；将正式 ToolSet 普通工具 parameters 与管理目录收敛到同一公开来源，保留 Provider 包装和结构化输出合同。不另建校验服务，按配置／发布／调用边界执行各自必要检查。
- 直接调整原执行函数的调用顺序：模型无关的工具构造与校验先于模型 SDK；依赖模型／CSM 的少量工具保留必要绑定位置，目录复用它们的参数声明。动态工具在一次 generation 中只构造一次，不引入 Prepared 协议、待绑定队列或阶段状态机。
- 压缩检查直接复用现有单模型构造入口，只准备原算法所需模型和凭据；显式 override_model 不再构造随后丢弃的配置模型。不重拆模型配置、指令或 Provider 层。
- 保留已有 Skill 派生、来源 metadata、上下文、控制状态和驻留状态查询边界；只迁移本次删除入口的真实调用方，不把已独立模块列为全面重构对象。受信 untrack 继续复用原无 Graph 的 CSM mutation。
- 沿用原 snapshot、publication、generation key、single-flight、封存、生命周期与错误合同；当前目录在已有共用发布屏障内捕获匹配的 active 配置与目录，构造留在锁外。冻结目录的原 cursor 失效与当前执行资格仍生效。只有实际取得资源的构造才登记现有 scope，不为全部 factory 新设生命周期接口、缓存或完整性检查。
- 以定向依赖测试和真实 Gateway／Workbench 入口验收完整 schema、本地失败、正常执行、模型回退和交互延迟，不新增全模块审计或生产监控。

取舍准则：即使 LiteLLM 明天消除首次导入耗时，每项必做改动仍应减少真实依赖、重复定义或未使用对象；仅为某次基准加速的框架化拆分不进入范围。

2026-11-25 收口裁定：用户明确选择保留剩余性能长尾 TODO、收口已完成功能。任务 5.2 的本地失败暖预算仍未达标，保持未勾选并随本变更归档；原 500／1500／3000ms 合同不放宽，不将延期写为验收通过。目录及其余功能的已完成范围保留，停止新增优化与重复性能采样。

## Capabilities

### New Capabilities

- `agent-capability-preparation`: 按领域与视图复用查询 owner、独立工具目录、共享定义与派生规则，以及模型无关工具的校验顺序和压缩检查最小依赖；保持唯一 Thread 执行链路。

### Modified Capabilities

无。既有配置、工具选择、来源生命周期及驻留执行合同继续有效。

## Impact

- 主链路：app/container.py、app/services/business/agent_tool_settings/、app/services/orchestration/agent_execution_service.py、app/runtime/agent_runtime.py、app/runtime/graph_factory.py、app/agents/agent_factory.py、app/agents/custom_tools.py、app/runtime/context_compaction.py 及直接／间接消费者和测试。
- 复用已有 model_tool_schema.py、参数模型、Provider 构造、策略投影与缓存 owner；共享函数能由已有文件承载就不新建文件，不增加 runtime 转发层。
- 查询消费者包括原 HTTP API、Workbench 设置与详情、模型工具及后端内部模块；协议层可以不同，同视图业务读取逻辑只保留一份。保持现有路径、DTO 和工作台操作，不新增聚合 API，不因内部重构升级协议或持久化格式。
- 配置采用 unify-configuration-inheritance 的权威 snapshot；错误采用 unify-failure-reporting 的原因、脱敏和 request_id 合同；性能预算不靠预热、改计时终点或筛样本放宽。
- 不改 Gateway、插件生命周期、审批、团队机制，及 python_exec、持续任务条件脚本、ffmpeg 的用法；不修改用户 Home，不自动同步、重启或提交。
