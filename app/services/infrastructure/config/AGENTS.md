# 目录用途

`app/services/infrastructure/config/` 存放配置基础设施的聚焦组件，包括不可变快照、文件变化监听、状态模型和 Workspace pending candidate 的待重启契约链路。

# 可修改内容

- 配置快照、fingerprint 与来源元数据。
- 配置文件目录监听和变化筛选。
- Workspace pending candidate 的启动契约、重启失败记录、重试、健康证明与提升/丢弃。

# 不可修改内容

- 不在本目录实现 Agent、MCP 或 Gateway 的业务编排。
- 不直接修改运行中的应用组件。
- 不在本目录反向持有 `ConfigService` 或通过 `self._service.xxx` 回调宿主。

# 规范

- 监听器只报告变化，候选配置的校验与提交由调用方负责。
- 配置加载失败必须向上抛出，不能吞掉异常或伪造成功状态。
- 待重启协作者的状态库和配置域必须通过构造参数显式注入，回读状态只允许通过注入的 reload status provider。
