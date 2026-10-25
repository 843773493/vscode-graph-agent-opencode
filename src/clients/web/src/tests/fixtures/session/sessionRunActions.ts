import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Session } from "../../../types/backend";
import type { AppState } from "../../../types/frontend";
import { useSessionActions } from "../../../hooks/session/useSessionActions";
import { createAppStateMirror } from "./appState";
import {
  testGatewayUserAccess,
  testWorkspaceRequestTargetsFor,
} from "./gatewayEnvironment";
import { createSessionCatalogOutboxTestRuntime } from "./sessionCatalogOutbox";

/**
 * 挂载 useSessionRunActions 并把最新 state 镜像到闭包。网关工作区标识从传入
 * state 派生，refreshAgentStateSnapshot 固定为空操作。
 */
export function mountSessionRunActions(options: {
  currentSession: Session | null;
  state: AppState;
  cacheKey: string;
}): { state: () => AppState; actions: ReturnType<typeof useSessionActions> } {
  const { currentSession, cacheKey } = options;
  const gatewayWorkspaceId = options.state.currentSessionWorkspaceId
    ?? options.state.activeGatewayWorkspaceId;
  const mirror = createAppStateMirror(options.state);
  let actions: ReturnType<typeof useSessionActions> | null = null;
  const selectionIntentRef = { current: 0 };
  const sessionCatalogOutboxRuntime = createSessionCatalogOutboxTestRuntime().runtime;
  function Harness(): React.ReactNode {
    actions = useSessionActions({
      apiPort: 8014,
      currentSession,
      activeGatewayWorkspaceId: gatewayWorkspaceId,
      currentSessionGatewayWorkspaceId: gatewayWorkspaceId,
      currentSessionCacheKey: cacheKey,
      defaultGatewayWorkspaceId: gatewayWorkspaceId,
      contentView: "default",
      readState: mirror.current,
      setState: mirror.setState,
      refreshAgentStateSnapshot: async () => undefined,
      abortCurrentStream: () => undefined,
      invalidateAgentState: () => undefined,
      sessionCatalogOutboxRuntime,
      gatewayUserAccess: testGatewayUserAccess(),
      selectionIntentRef,
      workspaceRequestTargets: testWorkspaceRequestTargetsFor(gatewayWorkspaceId),
    });
    return null;
  }
  renderToStaticMarkup(React.createElement(Harness));
  if (!actions) throw new Error("useSessionRunActions Harness 未完成渲染");
  return { state: mirror.current, actions };
}
