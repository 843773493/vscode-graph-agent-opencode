import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Session } from "../../../types/backend";
import type { AppState } from "../../../types/frontend";
import type { SetAppState } from "../../../hooks/contentViewLoaderTypes";
import { useSessionLifecycleActions } from "../../../hooks/session/useSessionLifecycleActions";
import type {
  SessionCatalogOutboxRuntime,
} from "../../../hooks/session/sessionCatalogOutboxRuntime";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { createAppStateMirror } from "./appState";
import {
  testGatewayUserAccess,
  testWorkspaceRequestTargetsFor,
} from "./gatewayEnvironment";
import { createSessionCatalogOutboxTestRuntime } from "./sessionCatalogOutbox";

type LifecycleActions = ReturnType<typeof useSessionLifecycleActions>;

/**
 * 挂载 useSessionLifecycleActions 并把最新 state 镜像到闭包。
 * 返回读 state 与动作引用的句柄，避免每个用例重写 Harness 与 setState 桥接。
 */
export function mountSessionLifecycleActions(options: {
  apiPort: number;
  currentSession: Session | null;
  workspaceId: string;
  state: AppState;
  abortCurrentStream?: () => void;
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime;
  workspaceRequestTargets?: readonly GatewayWorkspaceRequestTarget[];
  selectionIntentRef?: { current: number };
}): { state: () => AppState; actions: LifecycleActions; setState: SetAppState } {
  const {
    apiPort,
    currentSession,
    workspaceId,
    abortCurrentStream,
    sessionCatalogOutboxRuntime = createSessionCatalogOutboxTestRuntime().runtime,
  } = options;
  const mirror = createAppStateMirror(options.state);
  let actions: LifecycleActions | null = null;
  const selectionIntentRef = options.selectionIntentRef ?? { current: 0 };
  function Harness(): React.ReactNode {
    actions = useSessionLifecycleActions({
      apiPort,
      currentSession,
      activeGatewayWorkspaceId: workspaceId,
      currentSessionGatewayWorkspaceId: workspaceId,
      workspaceRequestTargets: options.workspaceRequestTargets ?? testWorkspaceRequestTargetsFor(workspaceId),
      defaultGatewayWorkspaceId: workspaceId,
      readState: mirror.current,
      setState: mirror.setState,
      abortCurrentStream: abortCurrentStream ?? (() => undefined),
      invalidateAgentState: () => undefined,
      sessionCatalogOutboxRuntime,
      gatewayUserAccess: testGatewayUserAccess(),
      selectionIntentRef,
    });
    return null;
  }
  renderToStaticMarkup(React.createElement(Harness));
  if (!actions) throw new Error("useSessionLifecycleActions Harness 未完成渲染");
  return { state: mirror.current, actions, setState: mirror.setState };
}
