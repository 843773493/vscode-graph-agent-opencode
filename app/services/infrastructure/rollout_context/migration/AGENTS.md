# 目录用途

`migration/` 只承载一次性 `legacy_import_v1_to_v2` 的 staging、report、quarantine 和 rollback audit。

# 可修改内容

- 可以只读解析 v1 artifact、生成 v2 migration plan 和审计报告。
- 可以调用 v2 writer 完成显式 import 安装。

# 不可修改内容

- 不得被正常 history/provider/checkpoint/runtime/context compiler 调用。
- 不得提供长期 compatibility API、dual writer 或 v1 fallback。

# 规范

- v1 source 只读、失败可审计；未知角色、版本、identity 或 hash 必须 quarantine/报错。
