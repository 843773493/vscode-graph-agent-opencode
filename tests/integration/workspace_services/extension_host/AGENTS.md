# 目录用途

保存 Workbench 通用窗口行为的回归测试；本目录不验证旧 VSIX／Extension Host 产品后端。

# 可修改内容

- 验证通用 CodeAction、Workspace Edit、Notebook 文档版本和扩展语言特性行为的测试。
- 测试应通过当前 Workbench API 或通用 transform 入口核对实际行为，并维持可区分正确实现与已知错误实现的断言。

# 不可修改内容

- 不恢复旧 VSIX 安装、扫描、市场、用户 Extension Host、RPC 或配置桥。
- 不在本目录新增已退役旧宿主产品链路的验收。

# 规范

- 复用真实通用入口和必要的原始 Workbench 类，不以目标宿主隔离装具替代产品行为。
- 运行测试须使用进程外超时和内存保护，产物放在对应 `out/tests/integration/workspace_services/extension_host/` 路径。
