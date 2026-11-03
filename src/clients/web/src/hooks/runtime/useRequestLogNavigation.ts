import { useEffect, useState } from "react";
import { locateLLMRequestLog, type LocatedLLMRequestLog } from "../../api/session/sessionMessages";
import type { LLMRequestLogMatchDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { errorMessage } from "../../utils/errorMessage";

export interface RequestLogNavigation {
  match: LLMRequestLogMatchDTO;
  location?: LocatedLLMRequestLog;
  error?: string;
}

/** 定位只持有本次原记录；切换 owner／命中时取消请求并释放旧正文。 */
export function useRequestLogNavigation(apiPort: number, workspaceId: string, sessionId: string,
  match: LLMRequestLogMatchDTO | undefined): RequestLogNavigation | undefined {
  const [state, setState] = useState<RequestLogNavigation>();
  useEffect(() => {
    setState(undefined);
    if (!match) return;
    const controller = new AbortController();
    const load = async () => {
      if (!match.reference || match.reference.session_id !== sessionId) throw new Error("请求日志命中与目标 Session 归属不一致");
      const location = await locateLLMRequestLog(apiPort, match.reference, workspaceId, controller.signal);
      if (!controller.signal.aborted) setState({ match, location });
    };
    void load().catch((error: unknown) => {
      if (!controller.signal.aborted) setState({ match, error: errorMessage(error) });
    });
    return () => controller.abort();
  }, [apiPort, workspaceId, sessionId, match]);
  return match ? state?.match === match ? state : { match } : undefined;
}
