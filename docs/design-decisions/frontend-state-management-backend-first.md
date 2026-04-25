# 前端状态管理模式：后端优先（Backend-First）

> **文档说明**：本文档总结 BoxTeam 扩展前端采用的状态管理策略，核心原则是**以后端为单一数据源**，前端仅作为展示层。

---

## 一、核心理念

### 基本原则
> **前端不维护业务状态的"真实来源"，所有状态变更必须通过后端API，并以后端返回的最新数据为准。**

### 数据流图
```
用户操作
   ↓
前端发送API请求
   ↓
后端处理并更新数据库
   ↓
后端返回更新后的完整资源对象
   ↓
前端用后端返回的数据**完全替换**本地状态
   ↓
前端重新渲染UI
```

### 对比：乐观更新 vs 悲观更新

#### ❌ 乐观更新（不推荐用于核心业务）
```javascript
// 先改前端，再调后端，失败回滚
this.state.session.agent_id = newAgentId;  // 立即更新UI
this.render();
try {
  await patchBackend(...);  // 异步请求
} catch (error) {
  // 失败需要回滚到旧状态
  this.state.session.agent_id = oldAgentId;
  this.render();
}
```
**缺点**：需要复杂的回滚逻辑，状态可能短暂不一致。

#### ✅ 悲观更新（推荐用于核心业务）
```javascript
// 先调后端，成功后以后端返回的数据刷新
try {
  const updated = await patchBackend(...);  // 等待后端返回
  this.state.session = updated;             // 整体替换，不部分修改
  this.render();
} catch (error) {
  await this.reloadFromBackend();  // 失败时从后端重拉，保证一致性
  showError(error);
}
```
**优点**：状态永远以后端为准，逻辑简单清晰。

---

## 二、核心原则

### 1. 单一数据源（Single Source of Truth）
后端是唯一真实数据源，前端不"猜测"或"假设"任何状态。

**❌ 错误示范：**
```javascript
// 前端自己修改ID，但后端可能失败或返回不同值
this.state.currentSession.agent_id = agentId;  // 自己改
await patchBackend(...);  // 后端可能失败
// 前后端状态已不一致
```

**✅ 正确示范：**
```javascript
// 等待后端返回完整对象，直接替换
const updated = await patchBackend(...);
this.state.currentSession = updated;  // 整体替换
```

### 2. 状态不可变性（Immutability）
不要部分修改对象属性，而是用后端返回的完整对象替换。

**❌ 错误示范：**
```javascript
this.state.sessions[index].agent_id = agentId;  // 只修改一个字段
this.state.currentSession.agent_id = agentId;
```

**✅ 正确示范：**
```javascript
this.state.sessions[index] = updatedSession;  // 完全替换
this.state.currentSession = updatedSession;
```

### 3. 失败恢复机制
任何API失败时，主动从后端重新拉取数据，确保前后端一致。

```javascript
try {
  const updated = await patchBackend(...);
  this.state.session = updated;
} catch (error) {
  // 失败时，从后端重新拉取最新状态
  await this.reloadFromBackend();
  this.postError(error);
}
```

---

## 三、适用场景

### 适用悲观更新的操作（推荐）
| 操作类型 | 原因 |
|---------|------|
| **切换Agent** | 业务核心状态，必须强一致 |
| **创建/删除Session** | 涉及数据库写入，ID由后端生成 |
| **发送消息** | 任务提交，需要后端确认job_id |
| **修改Session配置** | 配置持久化到后端 |
| **多端同步场景** | 避免并发冲突 |

### 可考虑乐观更新的操作（谨慎使用）
| 操作类型 | 原因 |
|---------|------|
| **标记已读** | 低风险，允许短暂不一致 |
| **收藏/点赞** | 可后续异步同步 |
| **UI状态切换**（面板展开/收起） | 纯前端状态，无需同步后端 |
| **草稿保存** | 本地操作，定时同步即可 |

---

## 四、实现模式

### 模式A：PATCH返回完整对象（推荐）
后端在PATCH请求中返回更新后的完整资源，前端直接替换。

**后端接口：**
```python
@router.patch("/{session_id}")
async def update_session(session_id: str, payload: SessionUpdateRequest):
    updated = await session_service.update(session_id, payload)
    return APIResponse(data=updated)  # 返回完整的SessionDTO
```

**前端调用：**
```javascript
async updateSessionAgent(sessionId, agentId) {
  try {
    const response = await fetch(`/api/v1/sessions/${sessionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ agent_id: agentId }),
    });

    const result = await response.json();
    const updatedSession = result.data;

    // 整体替换状态
    const index = this.state.sessions.findIndex(s => s.session_id === sessionId);
    if (index !== -1) {
      this.state.sessions[index] = updatedSession;
    }

    if (this.state.currentSession?.session_id === sessionId) {
      this.state.currentSession = updatedSession;
    }

    this.syncState();
  } catch (error) {
    await this.reloadSessions();  // 失败重拉
    this.postError(error);
  }
}
```

**优点**：一次请求完成更新和查询，效率高。

---

### 模式B：PATCH仅返回状态，额外GET
后端只返回成功/失败，前端需要额外调用GET获取最新状态。

**后端接口：**
```python
@router.patch("/{session_id}")
async def update_session(session_id: str, payload: SessionUpdateRequest):
    await session_service.update(session_id, payload)
    return {"message": "ok"}  # 不返回完整对象
```

**前端调用：**
```javascript
async updateSessionAgent(sessionId, agentId) {
  try {
    await patchBackend(...);  // 先更新

    // 再拉一次最新状态
    await this.reloadSessions();
  } catch (error) {
    await this.reloadSessions();
    this.postError(error);
  }
}
```

**缺点**：多一次网络请求，但后端实现简单。

---

## 五、错误处理策略

### 统一错误处理模式
```javascript
async updateSomething(id, data) {
  try {
    // 1. 发送API请求
    const updated = await patchBackend(...);

    // 2. 用后端返回更新前端
    this.state.items[id] = updated;
    this.syncState();

  } catch (error) {
    // 3. 失败时，主动从后端重拉数据
    await this.reloadFromBackend();

    // 4. 通知用户（但不中断流程）
    this.postError(error);
    showToast(`操作失败: ${error.message}`);
  }
}
```

### 失败场景处理

| 场景 | 处理方式 |
|-----|---------|
| **网络超时** | 重试机制 + 提示用户 |
| **后端返回错误** | 显示错误信息，重拉数据 |
| **数据格式错误** | 记录日志，重置为默认状态 |
| **并发冲突**（他人已修改） | 提示用户刷新页面 |

---

## 六、实际案例：Agent切换

### 完整代码示例
```javascript
// sidebarProvider.js
async updateSessionAgent(sessionId, agentId) {
  if (!sessionId || !agentId) {
    return;
  }

  this.log(`开始更新 session ${sessionId} agent 为 ${agentId}`);

  try {
    // 1. 调用后端 PATCH API
    const { port } = await this.ensureBackendReady();
    const url = `http://${DEFAULT_BACKEND_HOST}:${port}/api/v1/sessions/${sessionId}`;
    const response = await fetch(url, {
      method: 'PATCH',
      headers: {
        accept: 'application/json',
        'content-type': 'application/json',
        'X-Local-Token': DEFAULT_BACKEND_TOKEN,
      },
      body: JSON.stringify({ agent_id: agentId }),
    });

    if (!response.ok) {
      throw new Error(`PATCH failed: ${response.status}`);
    }

    // 2. 以后端返回的完整对象替换前端状态
    const result = await response.json();
    const updatedSession = result.data ?? result;

    const index = this.state.sessions.findIndex(s => s.session_id === sessionId);
    if (index !== -1) {
      this.state.sessions[index] = updatedSession;  // 整体替换
    }

    if (this.state.currentSession?.session_id === sessionId) {
      this.state.currentSession = updatedSession;
    }

    // 3. 通知webview刷新UI
    this.syncState(`已切换Agent为 ${agentId}`);

  } catch (error) {
    this.log(`更新失败: ${error.message}`);

    // 4. 失败时重新拉取session列表，保证前后端一致
    await this.reloadSessions();
    this.postError(new Error(`切换Agent失败: ${error.message}`));
  }
}
```

---

## 七、前端状态分类

### 类别1：后端同步状态（必须以后端为准）
- Sessions列表及当前Session
- Messages及Traces
- Agents列表
- Jobs状态

**处理方式**：所有变更走API，成功后**整体替换**。

### 类别2：前端本地状态（无需同步后端）
- 面板展开/收起状态
- 用户界面偏好（字体大小、主题）
- 临时输入内容
- 消息发送中的pending状态

**处理方式**：纯前端管理，定期持久化到 `vscode.setState()`。

### 类别3：混合状态
- pendingTurns：本地乐观显示，但需要后端确认后移除
- autoContinueEnabled：前端UI状态，但需同步到后端Session配置

**处理方式**：先乐观显示，API成功/失败后修正。

---

## 八、性能考虑

### 网络请求优化
| 策略 | 说明 | 适用场景 |
|-----|------|---------|
| **合并请求** | 一次PATCH返回完整对象 | 更新单个资源 |
| **批量获取** | `reloadSessions()` 统一刷新 | 失败恢复 |
| **缓存策略** | 内存缓存 + 定期失效 | 减少重复请求 |
| **乐观UI** | 显示loading但不阻塞 | 长时间操作 |

### 避免过度刷新
```javascript
// ❌ 错误：多次连续调用导致重复刷新
updateAgent('coder');
updateAgent('reviewer');  // 会触发两次reloadSessions

// ✅ 正确：最后一个请求覆盖前面的
let pendingUpdate = null;
async function updateAgent(agentId) {
  if (pendingUpdate) {
    clearTimeout(pendingUpdate);
  }
  pendingUpdate = setTimeout(async () => {
    await doUpdate(agentId);
    pendingUpdate = null;
  }, 300); // 防抖300ms
}
```

---

## 九、调试与监控

### 日志规范
```javascript
this.log(`开始更新 session ${sessionId} agent 为 ${agentId}`);
this.log(`后端返回: ${JSON.stringify(result)}`);
this.log(`session ${sessionId} agent 更新成功`);
```

### 状态一致性检查（开发环境）
```javascript
function assertStateConsistency() {
  const backendState = this.getBackendState();  // 从后端拉取
  const frontendState = this.state;

  if (JSON.stringify(backendState) !== JSON.stringify(frontendState)) {
    console.warn('前后端状态不一致，自动修复...');
    this.reloadFromBackend();
  }
}
```

---

## 十、总结

### 核心要点
1. **后端为单一数据源**：前端不维护业务状态的"真相"
2. **悲观更新优先**：核心业务走API → 后端返回 → 整体替换
3. **失败自动恢复**：出错时主动重拉，保证一致性
4. **状态不可变**：用新对象替换，不部分修改

### 适用项目
此模式特别适合：
- ✅ 本地AI助手类应用（如BoxTeam）
- ✅ 多端同步场景
- ✅ 业务逻辑复杂的系统
- ✅ 团队协作工具

### 不适用场景
- ❌ 纯前端UI组件状态管理
- ❌ 高频实时更新的仪表盘
- ❌ 低风险、可丢弃的操作（如点赞、标记）

---

## 参考文档
- [相关代码修改记录](../button-activation-bug-postmortem.md)
- [API接口规范](../reference/deepagent_backend_api_spec.md)
- [后端 Sessions API](../app/api/sessions.py)
