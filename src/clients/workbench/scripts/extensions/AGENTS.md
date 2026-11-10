# 目录用途

在唯一 sourceTransform 管线中连接自有插件管理入口、产品内置扩展与原 Workbench 通用行为修复。

# 可修改内容

固定上游的精确源码转换及实际转换文件清单；新增转换同步进入完整类型检查和构建。

# 不可修改内容

不得修改 upstream 原件、复制生产 Registry 或协议、恢复用户 VSIX／Gallery、目标 Extension Host 或旧配置桥。

# 规范

产品内置 WebWorkerExtensionHost 和原 Registry 保持；Browser 扩展发现只扫描产品 system／custom 内置项，管理接缝只读枚举。用户自有包由 servicePluginManagement 及原配置来源管理，不伪造 VSIX 对象。

插件管理沿原 Extensions 容器接自有包模型；只保留必要内置单例、容器、ext 快速访问及自有搜索／安装／刷新动作。MCP／AgentPlugins 独立视图使用原容器与查询 context，不得把这些查询当普通包名。欢迎页和语言选择器不再展示已退役市场入口或能力承诺。

文档、标签、语言、文件事件的转换只保留通用窗口修复，不推断旧 targetContext／host 归属。编辑版本、取消、预览、原 resolver 和 Provider 生命周期遵守实际业务合同；不得用移除旧目标检查替代真实版本及迟到回调保护。

工作区符号的明确范围在原 Provider 调用前核完整窗口工作区，保留原排序、接受、resolve 及错误回调。部分范围明确拒绝，不先查后过滤，不新建符号索引。

所有转换纳入正式构建与实际转换源码类型检查。测试与产物遵守根 AGENTS。
