# 目录用途

存放 Python 单元测试；目录优先镜像 `app/` 的生产模块，其他 Python 源根使用语义明确的独立分区。

## 可修改内容

- 不依赖真实进程、网络或图形客户端的 Python 单元测试。
- 单元测试专用 fixture、fake、stub 和断言辅助代码。

## 不可修改内容

- 不放完整 HTTP、Gateway、Workspace 后端或客户端链路测试。
- 不写入真实用户配置、真实工作区或项目根目录运行数据。

## 规范

- 测试路径镜像主要被测生产模块；跨越多个真实模块的测试应下沉到 `tests/integration/`。
- 依赖注入统一使用 pytest fixture，文件系统统一使用 `tmp_path` 或测试专用输出目录隔离。
- `tests/unit/` 根目录不放业务测试文件。
