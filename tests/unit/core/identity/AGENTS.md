# tests/unit/core/identity

## 目录用途

存放核心身份与标识单测，覆盖 UUID 标识符工厂、canonical ID 校验、身份 scope 编码及非 canonical 身份边界。

## 可修改内容

- 新增或修改 `app/core/` 身份与标识模块对应的单元测试。
- 添加仅服务于本目录测试的 fixture。

## 不可修改内容

- 不在此放置集成测试、e2e 测试或与身份无关的核心测试。
- 不在此实现生产身份逻辑；生产实现归属 `app/core/`。

## 规范

- 测试名应说明身份类型、输入条件和预期结果。
- 需要隔离工作区或测试产物时，路径应镜像本目录并写入 `out/tests/unit/core/identity/`。
- 使用仓库 pytest fixture 注入依赖，保持测试互相独立。
