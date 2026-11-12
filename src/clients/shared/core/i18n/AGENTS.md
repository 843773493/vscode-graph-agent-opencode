# 目录用途

保存启动语言已确定后的纯客户端翻译与格式化入口。

# 可修改内容

本目录的语言类型、翻译函数和本地资源目录。

# 不可修改内容

不得读取浏览器、Workbench、Electron 或持久化全局；不得在这里选择或保存语言。

# 规范

入口仅消费 bootstrap 提供的资源；英文原文只在英文 catalog 定义，中文资源按稳定 key 对应。`actionTitles.en.json` 与 `bootstrapMessages.json` 都由 Workbench build/typecheck 从权威 catalog 生成，不手工维护；前者只投影 Action 英文 original，后者只投影首屏启动所需的双语文案。保持同步初始化，不增加订阅或语言切换状态机。
