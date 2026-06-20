# Provider 自定义目录

## 设计原则

- 每个 provider 负责把**自己的方言**转换为**统一格式**
- 统一格式只关心 `AIMessageChunk` 的 `content` 和 `additional_kwargs["kind"]`
- `agent_execution_service` 不感知 provider 差异，只处理统一格式

## 统一格式约定

### `additional_kwargs` 字段：

```python
{
    "kind": "reasoning" | "text" | "tool",
    "phase": "start" | "delta" | "end",  # 仅用于 reasoning 标记边界
}
```

### 行为：

- `kind="reasoning"`：`content` 放推理过程文本，让前端显示为推理块
- `kind="text"`：`content` 放最终回复文本
- `kind="tool"`：标准 LangChain 工具调用格式，不改动
- `phase` 用于标记 reasoning 的开始和结束（start/end 时 content 可为空）

## 添加新 Provider 步骤

1. 在 `app/agents/providers/` 下新建 `xxx.py`
2. 继承 `ChatOpenAI`（或 `BaseChatModel`）
3. 重写 `astream` 方法，将原始流转换为统一格式
4. 在 `app/agents/agent_factory.py` 的 `build_runtime_for_agent` 中：
   ```python
   elif interface == "xxx":
       from app.agents.providers.xxx import XxxChatOpenAI
       model = XxxChatOpenAI(...)
   ```
5. **不需要修改 `agent_execution_service` 和前端**
