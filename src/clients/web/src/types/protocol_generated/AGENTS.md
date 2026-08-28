# src/clients/web/src/types/protocol_generated

## 目录用途

存放由 `.proto` 生成的 Web JSON 兼容 TypeScript 绑定，保留协议字段的 snake_case 线格式。

## 可修改内容

只能通过协议生成配置和 `bun run gen:protocol` 更新生成结果。

## 不可修改内容

不得手工修改生成的 TypeScript 文件，也不得把 Pydantic DTO 作为该目录的来源。

## 规范

生成绑定必须保持 `.proto` 的跨文件 import、字段编号和 JSON 名称；业务代码通过协议 adapter 使用这些类型。
