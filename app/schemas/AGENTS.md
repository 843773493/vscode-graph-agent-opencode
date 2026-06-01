# Directory Purpose

本目录存放后端接口与服务层使用的 Pydantic Schema / DTO 定义，负责定义请求、响应和内部传输结构。

# May Modify

- 各类请求/响应 DTO
- 与 API 返回强相关的 schema 文件
- 与服务层接口契约直接相关的类型定义

# Do Not Modify

- 业务逻辑实现
- 前端 webview 相关代码
- 与 schema 无关的运行时实现细节

# Conventions

- 优先按职责拆分文件，避免 `common.py` 继续膨胀
- 对外返回优先使用显式 DTO，避免裸 `dict`
- 新增子文件后优先让对应 API 路由直接引用它
- 保持字段命名与 API 实际返回一致
