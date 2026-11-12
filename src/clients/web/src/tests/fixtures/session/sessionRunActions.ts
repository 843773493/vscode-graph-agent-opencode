import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Session } from "../../../types/backend";
import type { AppState } from "../../../types/frontend";
import { useSessionLifecycleActions } from "../../../hooks/session/useSessionLifecycleActions";
import { useSessionRunActions } from "../../../hooks/session/useSessionRunActions";
import { createAppStateMirror } from "./appState";
import {

  testWorkspaceRequestTargetsFor,
} from "./gatewayEnvironment";
import { createSessionCatalogOutboxTestRuntime } from "./sessionCatalogOutbox";

type RunActions = ReturnType<typeof useSessionRunActions> & {
  selectSession: ReturnType<typeof useSessionLifecycleActions>["selectSession"];
};

/**
 * 挂载 useSessionRunActions 并把最新 state 镜像到闭包。网关工作区标识从传入
 * state 派生，refreshAgentStateSnapshot 固定为空操作。
 */
export function mountSessionRunActions(options: {
  currentSession: Session | null;
  state: AppState;
  cacheKey: string;
}): { state: () => AppState; actions: RunActions } {
  const { currentSession, cacheKey } = options;
  const gatewayWorkspaceId = options.state.currentSessionWorkspaceId
    ?? options.state.activeGatewayWorkspaceId;
  const mirror = createAppStateMirror(options.state);
  let actions: RunActions | null = null;
  const selectionIntentRef = { current: 0 };
  const sessionCatalogOutboxRuntime = createSessionCatalogOutboxTestRuntime().runtime;
  function Harness(): React.ReactNode {
    const lifecycle = useSessionLifecycleActions({
      apiPort: 8014,
      currentSession,
      activeGatewayWorkspaceId: gatewayWorkspaceId,
      currentSessionGatewayWorkspaceId: gatewayWorkspaceId,
      defaultGatewayWorkspaceId: gatewayWorkspaceId,
      readState: mirror.current,
      setState: mirror.setState,
      abortCurrentStream: () => undefined,
      invalidateAgentState: () => undefined,
      sessionCatalogOutboxRuntime,
      selectionIntentRef,
      workspaceRequestTargets: testWorkspaceRequestTargetsFor(gatewayWorkspaceId),
    });
    const run = useSessionRunActions({
      apiPort: 8014,
      currentSession,
      activeGatewayWorkspaceId: gatewayWorkspaceId,
      currentSessionGatewayWorkspaceId: gatewayWorkspaceId,
      currentSessionCacheKey: cacheKey,
      defaultGatewayWorkspaceId: gatewayWorkspaceId,
      contentView: "default",
      setState: mirror.setState,
      refreshAgentStateSnapshot: async () => undefined,
      createSession: lifecycle.createSession,
    });
    actions = { ...run, selectSession: lifecycle.selectSession };
    return null;
  }
  renderToStaticMarkup(React.createElement(Harness));
  if (!actions) throw new Error("useSessionRunActions Harness 未完成渲染");
  return { state: mirror.current, actions };
}
