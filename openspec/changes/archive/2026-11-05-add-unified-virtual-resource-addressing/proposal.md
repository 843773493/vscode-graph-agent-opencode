## Why

资源关联需要稳定、明确且可跨 Gateway 解析的目标。统一资源 URI 解决身份、地址、原生文件位置混用及平行解析器问题，但此前“原生路径只能在最终访问点出现”的规则过度限制了展示、诊断与原生操作，导致调试状态拒绝合法路径、stderr 隐藏和本机诊断难以定位文件。

2026-11-02 用户修订：资源关联与通用文件操作统一使用资源 URI；原生路径可以作为明确目标机器上的操作参数、展示信息、用户正文、诊断信息及必要的原生持久配置存在。消费者不得将其隐式解释为当前机器资源目标，也不得建立重复的权威定位关系。

## What Changes

- 统一 `boxteam://{gateway_authority?}/{scope}/{scope_id}/resources/{kind}/{...}` grammar、真实身份来源、能力与授权边界；scope 闭集为 workspace/user/gateway/inline，scope_id 全必填，文件 child 采用规范的一次 UTF-8 解码。
- 资源身份、资源 URI 与原生路径按字段职责分离。URI 字段继续拒绝非法 URI、错误归属和失权访问；普通正文、展示与诊断含路径本身不构成协议错误。秘密保护保持。
- 普通文件保留相对、绝对及 URI 输入，在明确目标端统一解析并复用同一操作链路；未注册目录仍可浏览编辑，终端／shell 保留原生语义。
- 调试状态允许目标机器原生展示路径，stderr 进入现有有界输出；可移植方案继续保留相对路径，导航与编辑仍用 URI。
- 移除按 metadata 键名递归禁止路径的启发式；明确的资源定位字段由所属 schema／owner 校验。
- 已知文件操作错误保留目标位置与具体原因；凭据、Provider 与内部校验等来源继续保护秘密，保留原错误归属和日志故障处理。
- 本机 doctor／diagnose 同时报告实际 JSONC、schema、SQLite 位置及既有 URI，保持只读；取代此前仅输出 URI 的裁定。
- Gateway 原始日志和 Browser file: URL 按各自管理授权与目标机器语义保留，不再需要因含路径登记少数例外。
- 保持唯一资源 binding、canonical owner、历史身份、revision 独立、星型联邦解析和错误码集中登记，不恢复旧路径别名、双读或第二定位链。

## Capabilities

### New Capabilities

- `virtual-resource-addressing`: 统一资源寻址、字段职责、目标机器解释、导航能力与联邦边界的唯一合同。

### Modified Capabilities

无；相关活跃变更引用本 capability，不复制 grammar 或职责判据。

## Impact

影响 Node Debug DTO／输出、Workspace 与资源 metadata、执行失败摘要、模型调用诊断、本机离线配置诊断及其测试。URI 格式、业务归属、权限和公开字段形状保持；doctor／diagnose 新增实际位置字段。受影响的旧路径禁止测试按新合同迁移，历史验收原件保留但不代替新规则验收。

2026-11-04 残留收口：文件 owner 的访问错误及历史 detail 文件错误仍有按旧策略删去 filename／具体原因的包装，按同一字段职责迁齐原调用方和测试；tasks 10.7 保持未完成。此前完成记录不等于全部错误出口已按新规则覆盖，不扩大为新的异常平台或全仓文本过滤。
