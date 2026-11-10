# 目录用途

将自有插件贡献接入原 Workbench 命令、菜单、快捷键和插件包管理入口，保留产品内置扩展的只读管理接缝。

# 可修改内容

自有包管理、贡献句柄及窗口订阅、明确工作区挂接命令、内置贡献枚举。

# 不可修改内容

不得建立第二份安装账本、业务资源定位关系或 VSIX 库存，不得恢复目标 Node／Web Extension Host、Gallery 安装与旧配置桥。

# 规范

`builtinManagement.ts` 只枚举原 scanner 的 system／custom 内置贡献；安装、卸载等用户操作明确拒绝，自有插件不伪造 ILocalExtension。产品内置 Worker、原 Registry 与原 FileService 仍保留；测试和临时产物遵守根 AGENTS。

自有包管理由 `servicePluginManagement.ts` 直接消费目标 Gateway 库存及原配置来源；Gateway、明确工作区与新建默认值各改原 owner 的 CAS 来源，不伪造 VSIX 或把 enabled 当 ready。工作区期望从原配置 owner 读取，异步交接状态另显；后台刷新以当前目标及代际结算。用户 VSIX、Gallery 与目标 Node／Web Extension Host 已退役，不恢复旧安装命令或配置桥。

自有命令由 `pluginCommandRegistry.ts` 持有原 Command／Menu／Keybinding 句柄，同逻辑 ID 只注册一个 dispatcher；完整快照先预校验再替换对应绑定，旧资格不得改投。`pluginCommands.ts` 负责窗口订阅与交互，传输归 Web `api/pluginCommands.ts`。命令面板在原 Quick Access 聚焦前冻结目标和已公布上下文，菜单保留自身冻结参数；程序化调用在首次 await 前捕获业务 owner。普通调用不经过旧宿主激活或重新发现；断连释放本目标投影，窗口关闭不修改 enabled。
