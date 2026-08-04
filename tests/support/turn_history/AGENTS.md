# 目录用途

存放长会话 Turn History 测试共享的确定性事件、投影、Trace 和 checkpoint 夹具构造代码。

## 可修改内容

- Turn 事件序列、展示投影和 Trace 文件的测试数据构造。
- 压缩前后 checkpoint 的确定性测试夹具。

## 不可修改内容

- 不放 Turn History 产品实现或测试用例。
- 不放与 Turn History 无关的通用消息、文本或进程 helper。

## 规范

- 事件构造、投影与 Trace 写入、checkpoint 压缩必须维持独立模块边界。
- 夹具必须确定性生成，不访问外部网络，也不写入调用方未明确传入的工作区。
- 新增公开 helper 时由测试从职责对应的具体模块导入，不提供旧模块兼容入口。
