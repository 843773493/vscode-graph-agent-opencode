import { useCallback, useEffect, useRef, useState } from "react";
import { clearSessionGoal, getSessionGoal, updateSessionGoal } from "../../api/session/sessionGoals";
import type { SessionGoal, SessionGoalUpdateRequest } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";

/** 详情只镜像 Goal API；任务 revision 变化时重新读取同一后端对象。 */
export function useTaskGoal(port: number, workspaceId: string, sessionId: string, revision?: number) {
  const [goal, setGoal] = useState<SessionGoal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const sequence = useRef(0);
  const writing = useRef(false);
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    setLoading(true);
    try {
      const value = await getSessionGoal(port, sessionId, workspaceId);
      if (request === sequence.current) { setGoal(value); setError(null); }
      return value;
    } catch (cause) {
      if (request === sequence.current) setError(errorMessage(cause));
      throw cause;
    } finally { if (request === sequence.current) setLoading(false); }
  }, [port, workspaceId, sessionId]);
  useEffect(() => {
    if (!writing.current) void refresh().catch(() => { /* 错误已呈现在详情。 */ });
    return () => { sequence.current++; };
  }, [refresh, revision]);

  const mutate = async <T,>(operation: () => Promise<T>): Promise<T> => {
    writing.current = true;
    sequence.current++;
    setLoading(true);
    let failure: unknown;
    try { return await operation(); }
    catch (cause) { failure = cause; throw cause; }
    finally {
      writing.current = false;
      // 成功与失败都读取后端完整对象，清除及并发修改不靠本地拼接。
      try { await refresh(); }
      catch (cause) {
        if (failure) throw new Error(`${errorMessage(failure)}；重新读取 Goal 失败：${errorMessage(cause)}`);
        throw cause;
      }
      if (failure) setError(errorMessage(failure));
    }
  };
  return { goal, loading, error, refresh,
    update: (payload: SessionGoalUpdateRequest) => mutate(() => updateSessionGoal(port, sessionId, payload, workspaceId)),
    clear: () => mutate(async () => { await clearSessionGoal(port, sessionId, workspaceId); }),
  };
}
