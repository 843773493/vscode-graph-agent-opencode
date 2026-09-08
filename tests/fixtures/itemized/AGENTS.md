# 目录用途

itemized schema 和 RFC 8785 跨 Python/Bun 的文本 golden。

# 可修改内容

可审查的 JSON 向量及独立 ESM 校验脚本。

# 不可修改内容

不得写入运行产物或引入生产实现的副本。

# 规范

冻结 canonical UTF-8 文本与摘要；校验失败直接报错，禁止校验器自动重写预期。正式运行产物归 out/tests/。
