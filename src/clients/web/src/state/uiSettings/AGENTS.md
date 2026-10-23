# 目录用途

集中定义 Web UI 持久化偏好的默认值、归一化与作用域转换。

## 职责树

```text
uiSettings/
├── preferences.ts                默认设置、载荷校验、游客补丁合并、稳定 ID 集合
├── uiSettingsPreferences.test.ts 归一化、错误边界与 ID 集合行为
└── uiSettingsGuest.test.ts       游客更新只替换本次返回的设置项
```

按设置数据流选择入口：服务端载荷先由 `normalizeWebUiSettings` 校验和补齐默认值；游客未持久化的更新由 `mergeGuestWebUiSettings` 依据本次 patch 合并；会话侧栏 ID 集合统一经稳定去重排序函数处理。新增设置时先明确 Gateway 持久化的权威字段，再把默认值、归一化与游客合并放在本目录对应链路中。

# 可修改内容

- Gateway UI 设置的纯数据解析与稳定集合转换。
- 会话侧栏、工作区文件树和 Gateway 控制台偏好的前端值对象。

# 不可修改内容

- 不直接调用后端 API 或操作 React 状态。
- 不保存 Composer 草稿或工作区业务数据。

# 规范

- 新增持久化偏好必须先在本目录声明默认值和作用域，再由统一控制器写入。
- 所有集合输出去重并稳定排序；代码注释使用中文，专业术语除外。
