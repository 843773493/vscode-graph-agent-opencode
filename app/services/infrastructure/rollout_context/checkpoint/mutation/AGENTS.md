# 目录用途

`mutation/` 是 SessionThread 唯一 mutation owner 的 typed intent 端口：intent 注册、
消费回执与重复消费拒绝，以及 Saver 侧的端口实现。

# 可修改内容

- 可以维护 mutation intent 的 typed 端口、消费回执与 owner/重复消费校验。
- 可以维护 Saver 组合用的 mutation intent port mixin 实现。

# 不可修改内容

- 不得建立第二套 mutation owner 或绕过 intent 直接写存储。
- 不得以自由 metadata key 代替 typed intent。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- intent 只能被其 owner 消费一次；重复消费与 owner 不匹配必须显式拒绝。
- 端口只承载类型与校验，不在此处实现持久化细节。

