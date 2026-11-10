# 目录用途

通过原 SettingsEditor／Command／Quick Input 编辑明确目标的 Code OSS 配置及工作区业务来源。

# 可修改内容

原设置输入的稳定目标、按编辑器组派生的配置服务和文件引用生命周期；默认 Agent 来源的选择、JSONC 单键编辑、来源预检和提交状态展示。`pluginConfiguration.ts` 持有自有插件唯一参数／绑定来源编辑流程，包详情与独立设置命令共用原预检、CAS 和私有输入链。

# 不可修改内容

不维护另一份配置真相，不将业务配置注册到 Code OSS 配置服务，不复制 HTTP 客户端或绕过来源 writer，不代替 Backend 重启与健康证明。

# 规范

冻结 Gateway／工作区／来源；只读来源仍可查看。复用 web API owner 和原 JSONC AST 编辑器，完整保留 CAS 与幂等身份；冲突保留输入，显式重新预检，未知结果仅查询原提交。使用中文提示与注释。

Code OSS 配置沿原 WorkspaceService、registry、JSON editing 与目标文件 Provider；当前工作台 profile 和目标 profile 分开显示。输入只保存稳定目标，不持有另一组的配置模型；每组 child 随原 Pane 关闭回收。JSON 文件模型持有独立传输引用，关闭设置 UI 不得截断 dirty 文件的保存。

设置标题只投影输入的冻结归属、准备时的目标配置写入授权和原文件服务已有状态；不为展示轮询或重复读取配置，也不接管保存。授权快照不代替实际保存时判权，普通文件的授权不能用独立的 `file_write` grant 策略代替；未知文件权限明确留给保存时检查。
