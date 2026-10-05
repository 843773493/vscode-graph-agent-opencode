import type { GatewayUserAccess, Session } from "../../types/backend";
import type { ConversationContentView } from "../../types/frontend";
import type { SetAppState } from "../contentViewLoaderTypes";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { useSessionLifecycleActions } from "./useSessionLifecycleActions";
import { useSessionRunActions } from "./useSessionRunActions";
import type { SessionCatalogOutboxRuntime } from "./sessionCatalogOutboxRuntime";

export function useSessionActions({
  apiPort,
  currentSession,
  activeGatewayWorkspaceId,
  currentSessionGatewayWorkspaceId,
  workspaceRequestTargets,
  currentSessionCacheKey,
  defaultGatewayWorkspaceId,
  contentView,
  setState,
  abortCurrentStream,
  invalidateAgentState,
  refreshAgentStateSnapshot,
  sessionCatalogOutboxRuntime,
  gatewayUserAccess,
}: {
  apiPort: number;
  currentSession: Session | null;
  activeGatewayWorkspaceId: string | null;
  currentSessionGatewayWorkspaceId: string | null;
  workspaceRequestTargets: readonly GatewayWorkspaceRequestTarget[];
  currentSessionCacheKey: string | null;
  defaultGatewayWorkspaceId: string | null;
  contentView: ConversationContentView;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateAgentState: () => void;
  refreshAgentStateSnapshot: (sessionId: string) => Promise<void>;
  sessionCatalogOutboxRuntime: SessionCatalogOutboxRuntime;
  gatewayUserAccess: GatewayUserAccess | null;
}) {
  const lifecycleActions = useSessionLifecycleActions({
    apiPort,
    currentSession,
    defaultGatewayWorkspaceId,
    activeGatewayWorkspaceId,
    currentSessionGatewayWorkspaceId,
    workspaceRequestTargets,
    currentSessionCacheKey,
    setState,
    abortCurrentStream,
    invalidateAgentState,
    sessionCatalogOutboxRuntime,
    gatewayUserAccess,
  });
  const runActions = useSessionRunActions({
    apiPort,
    currentSession,
    activeGatewayWorkspaceId,
    currentSessionGatewayWorkspaceId,
    currentSessionCacheKey,
    defaultGatewayWorkspaceId,
    contentView,
    setState,
    refreshAgentStateSnapshot,
  });

  return {
    ...lifecycleActions,
    ...runActions,
  };
}
