# 目录用途

保存 internal history 的冻结成员范围、typed carrier 完整性与 survivor 查询片段。

# 可修改内容

- 同一 snapshot/view 内的历史分页 SQL，由 catalog/internal_history.py 显式加载和协调。

# 不可修改内容

- 不新增 schema、迁移或持久化正文，不建立第二份历史事实源。
- 不按页锚点缩小替代关系的冻结范围。

# 规范

- 先按显式 membership 和 typed 关系择一，再排序与限量；页外替代仍参与判断。
- SQL 参数由既有 catalog owner 校验，Python 只读取有界元数据和正文。
