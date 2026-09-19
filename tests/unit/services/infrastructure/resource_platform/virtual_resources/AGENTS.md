# 目录用途

镜像 `app/services/infrastructure/resource_platform/virtual_resources/`，覆盖 VRN grammar、resolver、冻结绑定与纯值对象合同。

# 可修改内容

- grammar 拒绝表、scope/operation/capability 校验、历史冻结与同名覆盖、provenance 路径隐藏等单元测试。

# 不可修改内容

- 不读取真实文件系统或网络；resolver 拒绝路径不得触发任何 provider 访问。

# 规范

- 错误断言必须同时校验异常类型与 reason_code。
