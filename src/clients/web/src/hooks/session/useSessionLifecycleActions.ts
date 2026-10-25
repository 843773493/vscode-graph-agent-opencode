import { useEffect } from "react";
import type { SessionLifecycleContext } from "./lifecycle/sessionLifecycleContext";
import { useSessionAgentModelActions } from "./lifecycle/useSessionAgentModelActions";
import { useSessionCatalogActions } from "./lifecycle/useSessionCatalogActions";
import { useSessionCreationActions } from "./lifecycle/useSessionCreationActions";
import { useSessionSelectionActions } from "./lifecycle/useSessionSelectionActions";
import { useWorkspaceDefaultActions } from "./lifecycle/useWorkspaceDefaultActions";

export { refreshCurrentSessionCatalogOwner } from "./lifecycle/sessionCatalogOwnerRefresh";

export function useSessionLifecycleActions(context: SessionLifecycleContext) {
  // 仅在已提交的 owner 离开时作废旧请求；React 合并 A→B→A 时保留 A 的订阅。
  useEffect(() => () => context.invalidateAgentState(), [
    context.currentSession?.session_id,
    context.currentSessionGatewayWorkspaceId,
    context.invalidateAgentState,
  ]);

  const selectionActions = useSessionSelectionActions(context);
  const creationActions = useSessionCreationActions(context);
  const catalogActions = useSessionCatalogActions(context);
  const agentModelActions = useSessionAgentModelActions(
    context,
    creationActions.createSession,
  );
  const workspaceDefaultActions = useWorkspaceDefaultActions(context);

  return {
    createSession: creationActions.createSession,
    deleteSession: catalogActions.deleteSession,
    forkSessionContext: creationActions.forkSessionContext,
    renameSession: catalogActions.renameSession,
    setSessionParent: catalogActions.setSessionParent,
    selectSession: selectionActions.selectSession,
    selectWorkspaceSession: selectionActions.selectWorkspaceSession,
    switchAgent: agentModelActions.switchAgent,
    switchModel: agentModelActions.switchModel,
    setWorkspaceDefaultAgent: workspaceDefaultActions.setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider: workspaceDefaultActions.setWorkspaceDefaultProvider,
  };
}
