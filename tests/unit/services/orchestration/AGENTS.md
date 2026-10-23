# tests/unit/services/orchestration

## 目录用途

存放编排与流程控制服务的单元测试，例如任务执行、事件录制等。

## 职责树与选址

```text
orchestration/
├── communication/       Session 间消息接纳与冷观察的组合规则
├── resource_activation/ 资源快照冻结、binding 与 epoch 边界
└── test_<service>.py    app/services/orchestration 下单个服务的流程行为
```

优先让单个服务的测试文件与 `app/services/orchestration/` 同名模块对应；只有多个测试共同验证一个稳定子域、且该子域拥有独立依赖与规则时，才下沉到子目录并为其保留自己的 `AGENTS.md`。事件流、step 执行和 Session/Thread 生命周期等仍按各自服务文件组织，不因测试数量相近而混放。

## 可修改内容

- 新增、修改、删除编排服务的单元测试。
- 使用 fake/stub 替换下游依赖。

## 不可修改内容

- 不要测试具体业务规则。
- 不要引入真实 Agent 运行时或 LLM 调用。

## 规范

- 依赖注入优先使用 fixture 或构造函数传入 fake 实现。
- 异步流程应明确验证事件顺序和副作用。
