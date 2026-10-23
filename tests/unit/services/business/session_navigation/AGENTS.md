# tests/unit/services/business/session_navigation

## 目录用途

存放 `app/services/business/session_navigation/` 会话目录与物理导航规则的单元测试。

## 测试工作区数据树

```text
navigation_workspace/
└── .boxteam/
    ├── workspace-identity.json  从只读模板复制，固定测试工作区身份
    └── sessions/                交给 PathResolver 的隔离会话物理根
```

`test_operations_queue.py` 不读取项目源码或多媒体资产；新测试若只需要导航目录，应沿用 identity-only 工作区，不复制完整默认项目模板。只有被测行为确实读取模板文件时，才选择完整工作区 fixture。

## 可修改内容

- 会话目录服务、层级变更和索引一致性测试。
- 临时目录及导航 fixture。

## 不可修改内容

- 不绕过权威索引扫描并吸收磁盘变更。
- 不在此启动真实 Workspace HTTP 服务。

## 规范

- 物理树和权威索引必须同时断言。
- 文件系统测试必须使用 pytest 临时工作区。
