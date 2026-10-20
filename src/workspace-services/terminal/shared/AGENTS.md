# 目录用途

存放Terminal后端与attach网页已经共同消费的纯文本逻辑。

```text
shared/
├── commandText.js       命令标记、退出码、输入标签及展示过滤
└── commandText.test.js  同一份实现的文本行为检查
```

命令协议语义独占一份实现；server决定何时推导状态，client决定何时展示，shared不负责两者的调用时机。UTF-8字节边界仍归server/terminalUtf8Output.js，传输字段转换仍归protocol/messages.js。

## 可修改内容

- 接收字符串并返回派生值的命令文本算法及其单元测试。

## 不可修改内容

- 不持有Session、PTY、WebSocket、DOM或状态缓存。
- 不依赖Node专有模块、浏览器全局对象或生成另一套线格式。
- 不保留server/client旧路径的转发模块或重复算法。

## 规范

- 使用原生ESM，同一源码直接由Node及浏览器加载，不增加构建步骤。
- 展示过滤保留普通行的原始换行和ANSI，只删除包含命令标记的整行；不能修改权威原始输出。
- 测试按既有辅助服务单元套件发现，不复制到另一个运行面测试目录。
