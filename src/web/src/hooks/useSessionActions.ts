import type { Session } from "../types/backend";
import type { ConversationContentView } from "../types/frontend";
import type { SetAppState } from "./contentViewLoaderTypes";
import { useSessionLifecycleActions } from "./useSessionLifecycleActions";
import { useSessionRunActions } from "./useSessionRunActions";

export function useSessionActions({
  apiPort,
  currentSession,
  contentView,
  setState,
  abortCurrentStream,
  invalidateAgentState,
  refreshAgentStateSnapshot,
}: {
  apiPort: number;
  currentSession: Session | null;
  contentView: ConversationContentView;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateAgentState: () => void;
  refreshAgentStateSnapshot: (sessionId: string) => Promise<void>;
}) {
  const lifecycleActions = useSessionLifecycleActions({
    apiPort,
    currentSession,
    setState,
    abortCurrentStream,
    invalidateAgentState,
  });
  const runActions = useSessionRunActions({
    apiPort,
    currentSession,
    contentView,
    setState,
    refreshAgentStateSnapshot,
  });

  return {
    ...lifecycleActions,
    ...runActions,
  };
}
