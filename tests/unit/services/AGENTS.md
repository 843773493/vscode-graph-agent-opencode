# 目录用途

镜像 `app/services/`，存放业务服务、基础设施适配、映射和编排服务的单元测试。

## 可修改内容

- `business/`、`infrastructure/`、`mapping/`、`orchestration/` 对应的服务测试。
- 直接位于 `app/services/` 的模块所对应的测试。

## 不可修改内容

- 不启动真实 Workspace 后端、Gateway 或辅助服务进程。
- 不把跨多个真实服务边界的集成场景伪装成单元测试。

## 规范

- 子目录和测试文件镜像主要被测生产模块。
- 依赖通过 pytest fixture 注入；临时文件和持久化状态必须隔离。
