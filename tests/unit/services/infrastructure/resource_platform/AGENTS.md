# 目录用途

镜像 `app/services/infrastructure/resource_platform/`，存放资源观察平台的单元测试。

# 可修改内容

- 资源平台子目录的单元测试，按职责组织如下：

```text
resource_platform/
├── conftest.py                         # ResourceRegistry；EventChannelService 继承自父目录
├── derivation/                         # 无环派生、依赖代际与语义发布
├── observation/                        # 资源观察通道与事件游标
├── registry/                           # descriptor、快照 identity、CAS 与来源观察协调
├── sources/                            # 工作区、Gateway 资源来源与导航
├── virtual_resources/                  # URI grammar、resolver 与冻结绑定合同
├── test_resource_platform_architecture.py  # 跨模块静态边界审计
└── test_bootstrap.py                   # 资源平台初始化入口
```
- 留在本层的测试只覆盖跨模块架构审计或轻量初始化入口。

# 不可修改内容

- 不读取真实工作区文件或启动后端进程。
- 不把集成场景伪装成单元测试。

# 规范

- 子目录与测试文件镜像被测生产模块。
- 依赖通过 pytest fixture 注入。
