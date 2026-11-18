## Why

当前模型配置将一个 Provider 与一个模型绑定，同一服务下切换模型需要重复连接与凭据信息，工作区修改少量参数又会整体替换 Provider 数组。原生设置中的继承、参数来源和密钥复用也尚未形成一致体验；用户填写服务地址与密钥后，还缺少直接发现上游模型、保存前测试和解释失败原因的完整流程，需要统一为稳定身份的 Provider／Model 配置和 Gateway 持有的模型密钥。

## What Changes

- **BREAKING**：`llm.providers` 改为以不可变 Provider ID 为键的对象；旗下 `models` 同样以不可变 Model ID 为键。显示名称与服务端模型名称分离，不重复存储对象键中的 ID。
- Provider 提供连接、协议、凭据引用及可选请求参数默认值；Model 仅保存自身模型信息和显式覆盖。先合并配置来源，再解析 Provider 默认与 Model 覆盖，展示最终值及来源；两阶段的 reasoning 线格式声明均按完整块替换，协议切换不混合旧字段，其余选项保持原继承。
- **BREAKING**：Agent primary／fallback、SessionDefaults、会话选择、任务／子 Agent、测试及运行消费者统一使用结构化的 Provider＋Model 引用；同源私有 reasoning 回放同时校验实际模型与连接／认证来源，移除原仅 Provider ID 的选择和同源判定链。
- **BREAKING**：模型 API Key 的唯一持久化 owner 为所属 Gateway 的现有密钥库。Workspace／Backend 配置只存引用，不再接收模型密钥字面量；显式环境变量注入保留，不与密钥库自动回退。后端仅在模型请求准备与执行所需的内存中使用解析后的值。
- **BREAKING**：请求参数统一为 Provider 默认 → Model 覆盖 → Agent 显式参数 → 本次调用已支持的显式参数；替换现有 `request_options.overrides` 压过 Agent 参数的行为。先映射协议字段再合并，最终处理省略；拒绝 extra_body 与正式字段重复，不让 SDK 形成第二次覆盖。默认不填充采样、reasoning 或输出限制。
- 在原生 Settings 的“模型与 Provider”入口完成 Provider 分组、模型管理、来源检查、恢复继承和凭据复用；聊天模型选择器跳转同一配置模型，沿用已有 JSONC／schema／预检／CAS，不增加第二套管理页或草稿。
- 在当前连接表单首次填齐或连接信息改变并结束编辑时自动发现一次上游模型，保留手动刷新和手填；支持搜索、多选、目录差异与不完整结果，不自动导入、删除或覆盖已有模型。
- 提供独立于保存的草稿／已生效配置快速测试，分别验证流式和非流式文本调用；Provider 默认与 Model 覆盖通过 `api_mode.streaming` 控制真实上游传输，模型选择不再受单一流式假设限制。
- 测试显示真实目标、本页编辑与实际来源／active／凭据观察版本、逐项结果、首个有效内容与完整耗时、脱敏错误及字段定位；来源基线过期时零上游调用，已知配置或密钥变化使旧观察过期，不新增后台轮询。统一 POST SSE 的 start／delta／result、绝对期限和取消传播，缺终态不判通过，不自动恢复或重放推理请求；已有工具准确率测试作为深入入口，不由快速检查自动运行。
- 密钥创建在客户端提交前确定稳定 ID，响应丢失按原目标元数据恢复；更新／删除的未知结果与当前状态分开，不假定 vault 已有配置提交回执。临时 Key 明确绑定 Provider 或 Model 的凭据字段，替换后再解析，未生效或对象不匹配时网络前拒绝。
- 模型选择器支持分组搜索、最近使用、收藏、隐藏和进入对应设置，保留当前 Agent primary／fallback 的完整模型候选限制；Settings 管理全部工作区模型，偏好和发现不扩大执行集合。以复制模型配置提供参数预设，不另增 Variants 身份层。常用连接模板与高级字段渐进展开，显示最终请求地址和参数来源。
- 普通列表、打开页面与保存不触发上游发现或推理，不解密全部凭据；目录发现仅随当前连接编辑或显式刷新发生，推理测试始终显式启动，可取消且不隐式重试。保存、active、测试通过与后续 Job 生效分别呈现。
- 在同一纵向切片迁齐协议、持久引用、测试和全部直接／间接消费者后删除旧格式处理；迁移仅按已核验 release 契约或用户另行授权，不为开发中间态新增双读或自动修复。

## Capabilities

### New Capabilities

- `provider-model-configuration`：稳定 ID 对象结构、两种继承关系、请求参数与流式规则、结构化引用、目录发现、草稿测试及后续 Job 生效。
- `gateway-model-credentials`：模型密钥的 Gateway 归属、引用复用、环境来源、轮换／删除、执行与草稿探测的短暂使用和展示边界。

### Modified Capabilities

- `harness-agent-configuration-ui`：替换平铺 Provider、数组覆盖与 Workspace 密钥字面量约定；统一原生设置、凭据选择、连接引导、目录多选、草稿测试、Agent 引用与日常模型选择。

## Impact

- 配置与运行：`configs/workspace_*.jsonc`、配置加载／生效策略、原配置 API、`app/services/infrastructure/config/`、`config_service/`、`app/runtime/provider_model.py`、现有模型 adapter 及 Saver／wire finalizer 的请求封存与发送一致性链；新增发现／测试为工作区业务请求，Gateway 透明路由。
- 凭据：复用 `app/gateway/secrets/`、Gateway secrets 路由与 `app/services/infrastructure/gateway_secrets/`；创建协议改为必填客户端预定 ID，保留原 writer 和 revision CAS，不新增操作回执库、密钥库、SQLCipher、系统钥匙串或工作区密钥 writer；不迁移插件秘密及 OAuth owner。
- 引用与协议：Agent、SessionDefaults、会话当前选择、Job／任务／子 Agent、模型工具测试和诊断的原 owner；Protobuf／生成客户端及受影响存储合同须同步检查，不扫描或重写全历史。
- 前端：`src/clients/workbench/src/settings/`、复用的 React 设置实体与模型选择器；Web／Electron 使用同一源码、业务 URI 和保存链。
- 参考：`reference_repo/vscode` 的 `eb50e1bf17fc87a192079dda6ab5d2b6523ee68c`，重点为 Copilot Custom Endpoint、模型参数合并和原生模型管理。参考更新不升级产品固定 upstream。
- 体验参考：OpenCode `df46f6931da0c934a59d375ac44525846f21c718` 的模型选择与专用目录发现；New API `ce8fc49e4b1a874aa917dce4f1a8416c52ec8ec0` 的草稿发现、模型差异和协议／流式测试。只借鉴适用交互，不引入渠道运营、计费、自动禁用或后台巡检体系。
- 验收：少量真实 Gateway／Workspace／Workbench 纵向场景，复用现有模型测试服务并执行既有内部总延迟预算。
