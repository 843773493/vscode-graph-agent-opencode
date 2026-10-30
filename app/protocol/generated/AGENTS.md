# 目录用途

存放由 Buf 生成的 Python Protobuf 绑定及类型声明。

## 可修改内容

- 仅允许通过 `bun run gen:protocol` 更新生成结果。

## 不可修改内容

- 不得手工修改生成的 Python 文件或 `.pyi` 类型声明。

## 规范

- 生成文件必须与 `proto/` 的相对路径对应。
