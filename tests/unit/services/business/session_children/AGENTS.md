# 目录用途

`session_children/` 覆盖 Session child-thread 的读取与发布生命周期：列表读模型负责从权威控制存储投影 child 状态，Team board 只展示已发布 member，创建 owner 校验 coordinator 身份。

## 测试职责树

```text
session_children/
├── conftest.py                       隔离 catalog workspace 与 SessionService
├── helpers.py                        child creation、owner 与控制存储共享装具
├── test_child_thread_read_model.py   child 列表、排序、状态和发布快照
├── test_team_board_publication.py    registering/published/cancelled member 投影
└── test_child_creation_owner.py      coordinator 身份与 owner 隔离
```

# 可修改内容

- child-thread 读模型、Team board 发布可见性和创建 owner 合同测试。
- 本目录专属的临时 workspace fixture 与 fake。

# 不可修改内容

- 不测试 Session 导航树、Job 调度或通用 Team store 持久化；这些合同属于对应目录。
- 不启动真实外部服务，也不写入仓库根目录或用户工作区。

# 规范

- workspace 与 SessionService 保持函数级隔离；并发发布测试必须保留独立 reader/writer 屏障。
- 测试按读模型、发布投影和创建 owner 分文件；共享构造逻辑放在 `helpers.py`。
