# 目录用途

`app/services/business/` 放真正的业务服务实现，主要负责状态管理、业务规则、领域动作和对外提供的业务能力。

## 可修改内容

- 可以新增、修改、删除业务服务实现文件。
- 可以调整业务服务内部依赖，但应继续依赖 `app/abstractions/` 中的协议，而不是直接耦合容器或 API 层。

## 不可修改内容

- 不要把流程编排、事件推送、运行时组装放进这里。
- 不要把纯基础设施能力放进这里。

## 规范

- 保持每个文件单一职责。
- 出现业务失败时直接抛出明确错误，不要静默降级。
- 冷读在 topology shared 下取得 Session shared guard 并确认 catalog active 后立即释放 topology；Session guard 覆盖全部读取句柄。child thread 列表及后续团队/运行态投影由 API 持有同一次读准入，内部查询不嵌套取得 topology。
- 优先通过协议隔离依赖，避免循环引用。
