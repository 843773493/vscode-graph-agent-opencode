import type { AppState } from "../../types/frontend";

/**
 * 清空会话级附属缓存显示态：Trace 事件、LLM 请求日志与会话资源。
 *
 * 打开/创建/删除会话切换 currentSession 时，这些缓存属于旧会话，必须与
 * currentSession 一起复位。此前 useSessionLifecycleActions 与
 * useSessionRunActions 各自复制了逐字相同的 9 行赋值，收敛到这里后只有一份。
 *
 * 与 resetAgentStateFields 保持一致：返回带新值的 state 副本，调用方用
 * `Object.assign(next, resetSessionScopedFields(next))` 落回。
 */
export function resetSessionScopedFields(state: AppState): AppState {
  return {
    ...state,
    traceEvents: [],
    llmRequestLogs: [],
    llmRequestLogsLoadedAt: null,
    llmRequestLogsLoading: false,
    llmRequestLogsError: null,
    sessionResources: [],
    sessionResourcesLoadedAt: null,
    sessionResourcesLoading: false,
    sessionResourcesError: null,
  };
}

