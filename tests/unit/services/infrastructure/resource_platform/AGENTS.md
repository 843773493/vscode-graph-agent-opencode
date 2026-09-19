# 目录用途

镜像 `app/services/infrastructure/resource_platform/`，存放资源观察平台的单元测试。

# 可修改内容

- 对应资源平台子目录（sources、observation、registry、virtual_resources 等）的单元测试。

# 不可修改内容

- 不读取真实工作区文件或启动后端进程。
- 不把集成场景伪装成单元测试。

# 规范

- 子目录与测试文件镜像被测生产模块。
- 依赖通过 pytest fixture 注入。
