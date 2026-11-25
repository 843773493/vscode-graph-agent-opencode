## Why

现有派生执行已经复用任务注册器、internal execution 和统一模型发送，但公共字段、输出校验及日志仍绑定压缩。需要原位抽取可参数化的执行入口，避免未来每种用途另写发送器。禁止实际工具操作也不应清空来源工具定义、无谓破坏缓存前缀。

## What Changes

- 将压缩专用公共引用扩展为 typed 派生引用，复用原准入、图工厂、模型发送、工具节点、任务和存储。
- 提供一个生产可用的参数化入口：来源、任务提示词、工具允许列表、模型选择、期限及原调用上限。本次返回文本；输出要求写在任务指令中，严格结构化输出留待按原协议扩展。
- 参数在所属入口解析，主会话／派生调用绑定在图装配时选定；共同封存与发送只消费统一准备结果，不逐层透传is_derived／purpose来切换业务行为，不新增策略注册框架。
- 保留可复用的来源 root、工具定义和连续前缀，独立限制实际工具执行；不默认改 tools 或 tool_choice。
- 按原 task 身份幂等受理，冻结来源与配置；一个 task 可以关联多个 execution，各 execution 可以包含多个模型／工具步骤。
- 普通派生优先以已有适用的ready摘要替换对应来源，其余内容完整保留；没有则直接使用原内容，不等待或生成摘要。容量检查复用原入口，不新增输入token预算参数，超限失败沿原任务和Trace留痕。
- 沿原注册器保留后台一项运行、四项等待，增加一个总运行上限2；不新增交互配额、队列服务、期限定时器或运行状态镜像。
- 暂停／打断接入已有控制边界：派生不继承主执行暂停对象；interrupt／stop 按原 SessionStopPolicy 处理后台资源，显式取消与删除沿原入口。
- 任务结果、恢复、保留和删除沿既有 owner，先保存完整结果再确认 task；未知模型请求不自动重发，未决工具操作不丢失。
- 工作区提供统一提交／查询／取消接口，Gateway透明路由，常驻更新复用统一实时通道。
- **BREAKING**：迁齐后删除公共 compaction-only 引用、固定摘要日志／输出校验、空工具集强制路径和同 Thread 任意派生合并，不保留两套公共执行实现。

## Capabilities

### New Capabilities

- `derived-execution`：复用既有执行基础的临时派生请求、缓存与工具约束、任务控制、结果及数据归属合同。

### Modified Capabilities

无独立主规范 delta。现有压缩 change 接入共同合同，保留自己的生成、候选和采用策略。

## Impact

调整原 execution admission、model_call_execution、model_call_graph、AgentToolNode、BackgroundTaskRegistry、rollout、日志及工作区路由；不新增 JobService、执行循环、权限库或内容库。

压缩是本次唯一产品业务接入。会话／Thread命名、不同频率的用户／项目／会话记忆草稿、向繁忙Thread临时提问或调用部分工具，仅用于检验接口扩展性；不实现这些业务的 handler、触发器、存储、UI 或团队协议。

公共实现任务归本 change，压缩接入归 `prepare-context-compaction-in-background`；保留其并行改动及真实验收证据。共同 capability 先归档，压缩业务后归档。持久化必要变更由原 owner 登记版本，按既有支持来源处理，不自动转换真实 dev 数据。
