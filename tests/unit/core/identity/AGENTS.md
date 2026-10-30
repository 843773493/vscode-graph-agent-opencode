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
- `test_workspace_identity_errors.py`核真实身份文件损坏、非文件、绑定冲突及修复后的原入口重试；原生访问故障只注入最终I/O边界，分别核类别、错误码、直接cause和诊断原文。并发创建场景由真实O_EXCL冲突读回原文件，不手填产品恢复状态；Linux上的Windows/UNC错误正文取样不等于对应平台实机验收。
- `test_workspace_identity_descriptors.py`核真实O_EXCL创建后的fdopen故障与取消：描述符由创建者关闭一次，收尾再次失败仍保留两份原因，残留文件继续由原入口明确拒绝；装具自身必须回收失败实现泄漏的描述符。
