# 目录用途

存放由 Buf 生成的 Terminal/Browser Node.js ESM 绑定。

## 可修改内容

- 仅允许通过 `bun run gen:protocol` 更新生成结果。

## 不可修改内容

- 不得手工修改生成的 `.js` 或 `.d.ts` 文件。

## 规范

- 输出目录必须保留 `boxteam/<domain>/v<version>` 的源协议层级。
