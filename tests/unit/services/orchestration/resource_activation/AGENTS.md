# 目录用途

存放 ResourceActivationCoordinator 和 typed activation snapshot 的单元合同测试。

# 可修改内容

- 资源激活 policy、Turn/ModelCall snapshot、唯一 Saver port 与不可漂移验收。

# 不可修改内容

- 不在本目录测试文件读取、目录枚举、网络请求或第二持久化 writer。

# 规范

- 测试必须直接构造 typed ResourceRegistry snapshot；不通过 provider locator。
