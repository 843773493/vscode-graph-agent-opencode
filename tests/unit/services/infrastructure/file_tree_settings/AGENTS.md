# tests/unit/services/infrastructure/file_tree_settings

## 目录用途

存放 `app/services/infrastructure/file_tree_settings_service.py` 的会话及工作区快捷方式单元测试。

## 可修改内容

- 快捷方式读取、写入、默认值快照与迁移测试。
- 持久化错误、无效内容诊断和原子文件替换测试。

## 不可修改内容

- 不测试工作区文件树扫描、导航或正文读写。
- 不改写 `tests/fixtures/workspaces/` 模板，也不使用真实工作区目录。

## 规范

- 工作区由 `empty_workspace_scope` fixture 隔离，测试产物路径从当前测试文件派生。
- 文件错误断言保留原错误类型、原因和文件原子性边界。
