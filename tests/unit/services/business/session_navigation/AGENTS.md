# tests/unit/services/business/session_navigation

## 目录用途

存放 `app/services/business/session_navigation/` 会话目录与物理导航规则的单元测试。

## 可修改内容

- 会话目录服务、层级变更和索引一致性测试。
- 临时目录及导航 fixture。

## 不可修改内容

- 不绕过权威索引扫描并吸收磁盘变更。
- 不在此启动真实 Workspace HTTP 服务。

## 规范

- 物理树和权威索引必须同时断言。
- 文件系统测试必须使用 pytest 临时工作区。
