# 目录用途

将已提交的有序历史消息和索引字段转换为 content、tool 和 Turn DTO。

# 可修改内容

- 无 I/O 的内容、工具、摘要、详情和 include 投影。
- 调用方提供的单次请求预算与可见字段过滤。

# 不可修改内容

- 不得读取 storage、文件、数据库、容器或当前工具 registry。
- 不得推断 Turn root、补造 final pointer 或重新排列 canonical 输入。

# 规范

- 正文与 identity 来自调用方的同一已提交 snapshot。
- 保留 carrier 顺序；未请求的字段不解析，已请求的缺失事实必须报错。
