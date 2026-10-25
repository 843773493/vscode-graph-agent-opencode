# tests/unit/services/infrastructure/resource_platform/sources

## 目录用途

存放 `app/services/infrastructure/resource_platform/sources/` 工作区和 Gateway 资源来源、导航的单元测试。

## 可修改内容

- 工作区资源读取、导航与来源映射测试。
- Gateway 资源导航、skill 快照和观察边界测试。
- 使用内存对象或临时路径构造的测试数据与 fixture。

## 不可修改内容

- 不启动真实 Gateway、工作区后端或外部网络请求。
- 不重复测试资源注册表、观察通道和虚拟 URI parser 的内部合同。

## 规范

- 依赖通过 pytest fixture 注入；Gateway 传输必须使用测试替身。
- 工作区文件读写限制在 `tmp_path` 下。
