import { useEffect, useState } from "react";
import { locateTrace, type LocatedTrace } from "../../api/session/sessionMessages";
import type { TraceMatchDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { errorMessage } from "../../utils/errorMessage";

export interface TraceNavigation {
  match: TraceMatchDTO;
  location?: LocatedTrace;
  error?: string;
}

/** 命中归属固定到精确 Thread；取消或切换时释放本次单记录原文。 */
export function useTraceNavigation(apiPort: number, workspaceId: string, sessionId: string, threadId: string,
  match: TraceMatchDTO | undefined): TraceNavigation | undefined {
  const [state, setState] = useState<TraceNavigation>();
  useEffect(() => {
    setState(undefined);
    if (!match) return;
    const controller = new AbortController();
    const load = async () => {
      if (!match.reference || match.reference.session_id !== sessionId || match.reference.thread_id !== threadId) {
        throw new Error("Trace 命中与目标 Session／Thread 归属不一致");
      }
      const location = await locateTrace(apiPort, match.reference, workspaceId, controller.signal);
      if (!controller.signal.aborted) setState({ match, location });
    };
    void load().catch((error: unknown) => {
      if (!controller.signal.aborted) setState({ match, error: errorMessage(error) });
    });
    return () => controller.abort();
  }, [apiPort, workspaceId, sessionId, threadId, match]);
  return match ? state?.match === match ? state : { match } : undefined;
}
