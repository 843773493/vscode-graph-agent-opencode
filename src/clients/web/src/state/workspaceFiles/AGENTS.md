# 目录用途

保存工作区文件操作的未确认 intent 与上传 Blob；IndexedDB 是客户端待确认记录的唯一持久存储，dispatcher 统一负责发送、查询和终态等待。

## 可修改内容

- intent、依赖引用及上传 Blob 的 IndexedDB 读写、删除和数据类型。
- 冻结 Gateway、工作区、资源根和请求身份的本地记录。
- dispatcher 与 intent store 直接相关测试。

## 不可修改内容

- 不在 intentStore、文件树 hook 或 Workbench provider 中维护第二套 HTTP 发送、状态轮询或终态解释；统一消费 dispatcher。
- 不决定后端文件 revision、操作成功、冲突或恢复结果。
- 不保存第二份文件业务目录或注册逐文件长期身份。

## 规范

- 发送前持久保存完整 intent；dispatcher 未确认终态时保留原 identity 与 target。刷新、重连和工作区切换不得改写或删除它们。
- 文件树 hook 与 Workbench provider 共用 dispatcher；只有明确终态才能完成各自的展示协调或删除终态已确认记录。
- 上传 Blob 与 intent 在同一存储合同下保存；清理以明确的后端终态回执或用户取消为依据。
- 本地存储失败必须可见；不伪报已提交或已完成。
- 代码注释使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
