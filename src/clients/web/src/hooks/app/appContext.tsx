import React, { createContext, useContext } from "react";
import type { TurnHistoryInclude } from "../../api/session/sessionTurnHistory";
import type {
  AddManagedGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  GatewayRuntimeRestartResult,
  AttachmentRef,
  MessageReplayRequest,
  DeliveryPolicy,
  SessionResourceAction,
  SessionResourceKind,
  SessionFileChange,
  Session,
  SessionCompactResult,
  SessionGoal,
  SessionGoalUpdateRequest,
  SessionChangesetList,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";
import type {
  AppState,
  ConversationContentView,
} from "../../types/frontend";
import type { SessionViewStatePayload } from "../session/useSessionViewState";
import type { ComposerStateSnapshot } from "../../state/composerState";

export interface AppContextType {
  state: AppState;
  setStatus: (text: string) => void;
  sendMessage: (
    content: string,
    attachments?: AttachmentRef[],
    deliveryPolicy?: DeliveryPolicy,
  ) => Promise<void>;
  updatePendingRequest: (
    messageId: string,
    content: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  removePendingRequest: (messageId: string) => Promise<void>;
  clearPendingRequests: () => Promise<void>;
  updatePendingRequestPolicy: (
    messageId: string,
    deliveryPolicy: DeliveryPolicy,
    expectedSnapshotVersion?: number,
  ) => Promise<void>;
  loadAroundTurn: (anchorTurnId: string) => Promise<void>;
  loadNewerMessages: () => Promise<void>;
  loadOlderMessages: () => Promise<void>;
  loadTurnDetails: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ) => Promise<void>;
  loadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
  refreshTurnHistory: () => void;
  loadOlderTraceHistory: () => Promise<number>;
  refreshTraceHistory: () => Promise<void>;
  /** 重新读取当前会话的 LLM 请求响应日志；请求视图加载失败的重试入口复用同一实现。 */
  refreshLLMRequestLogs: (sessionId: string) => Promise<void>;
  replayTurn: (
    targetMessageId: string,
    action: MessageReplayRequest["action"],
    displayContent: string,
    content?: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  compactSession: () => Promise<SessionCompactResult>;
  refreshGoal: () => Promise<SessionGoal | null>;
  updateGoal: (
    payload: SessionGoalUpdateRequest,
    target?: { sessionId: string; workspaceId: string | null },
  ) => Promise<SessionGoal>;
  clearGoal: (
    target?: { sessionId: string; workspaceId: string | null },
  ) => Promise<void>;
  switchAgent: (agentId: string) => Promise<void>;
  switchModel: (providerId: string) => Promise<void>;
  refreshAgents: (workspaceId: string) => Promise<void>;
  setWorkspaceDefaultAgent: (agentId: string) => Promise<void>;
  setWorkspaceDefaultProvider: (
    agentId: string,
    providerId: string,
  ) => Promise<void>;
  interruptSession: () => Promise<void>;
  selectSession: (sessionId: string) => void;
  selectWorkspaceSession: (
    workspaceId: string,
    sessionId: string,
    sessionOverride?: Session,
  ) => void;
  openWorkspaceSession: (workspaceId: string, sessionId: string) => Promise<void>;
  createSession: (
    title?: string,
    workspaceId?: string | null,
    folderId?: string | null,
  ) => Promise<Session>;
  forkSessionContext: (
    workspaceId: string,
    sourceSessionId: string,
  ) => Promise<void>;
  renameSession: (
    sessionId: string,
    title: string,
    workspaceId?: string | null,
  ) => Promise<void>;
  deleteSession: (
    sessionId: string,
    workspaceId?: string | null,
  ) => Promise<void>;
  setSessionParent: (
    workspaceId: string,
    sessionId: string,
    parentSessionId: string | null,
  ) => Promise<void>;
  refreshSessionResources: (
    sessionId: string,
    options?: { silent?: boolean },
  ) => Promise<void>;
  controlSessionResource: (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => Promise<void>;
  loadSessionChangesets: (sessionId: string) => Promise<SessionChangesetList>;
  refreshSessionChanges: (
    sessionId: string,
    changesetId?: string | null,
    options?: { refreshList?: boolean },
  ) => Promise<void>;
  reviewSessionChangeFile: (
    file: SessionFileChange,
    reviewed: boolean,
  ) => Promise<void>;
  toggleAgentSessionsPanel: () => void;
  toggleExpandDetails: (expand: boolean) => void;
  switchContentView: (view: ConversationContentView) => void;
  activateGatewayWorkspace: (
    workspaceId: string,
    preferredSessionId?: string | null,
  ) => Promise<void>;
  refreshGatewayState: () => Promise<void>;
  reconnectGatewayWorkspace: (workspaceId: string) => Promise<void>;
  safeRestartManagedGatewayWorkspaceBackend: (
    workspaceId: string,
  ) => Promise<GatewayRuntimeRestartResult>;
  forceRestartManagedGatewayWorkspaceBackend: (
    workspaceId: string,
  ) => Promise<GatewayRuntimeRestartResult>;
  startManagedGatewayWorkspaceBackend: (workspaceId: string) => Promise<void>;
  stopManagedGatewayWorkspaceBackend: (workspaceId: string) => Promise<void>;
  probeExternalGatewayWorkspace: (workspaceId: string) => Promise<void>;
  addManagedGatewayWorkspace: (
    payload: AddManagedGatewayWorkspaceRequest,
  ) => Promise<void>;
  addSshGatewayWorkspace: (
    payload: AddSshGatewayWorkspaceRequest,
  ) => Promise<void>;
  removeGatewayWorkspace: (workspaceId: string) => Promise<void>;
  renameGatewayWorkspace: (workspaceId: string, name: string) => Promise<string>;
  setGatewayWorkspaceParent: (
    workspaceId: string,
    parentWorkspaceId: string | null,
  ) => Promise<void>;
  refreshGatewayWorkspaceSessions: (workspaceId: string) => Promise<void>;
  reorderGatewayWorkspaces: (workspaceIds: string[]) => Promise<void>;
  copySessionInformation: (workspaceId: string, sessionId: string) => Promise<void>;
  copyWorkspaceInformation: (workspaceId: string) => Promise<void>;
  updateUiSettings: (
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ) => Promise<void>;
  saveSessionViewState: (payload: SessionViewStatePayload) => void;
}

type HotReloadContextStore = {
  appContext?: React.Context<AppContextType | null>;
  composerContext?: React.Context<ComposerContextType | null>;
};

// Vite 热更新会分别重载 Provider 和消费者模块；复用 Context 身份，避免
// 旧 AppShell 读取到新 AppProvider 之外的 Context。生产构建不依赖这段状态。
const hotReloadContextStore = import.meta.hot?.data as HotReloadContextStore | undefined;
// 与 ComposerContext 同形显式导出：测试需要直接用真实 Provider 注入 AppState，
// 而不是替换整个 hooks 模块（bun 的 mock.module 是进程级且不可撤销）。
export const AppContext = hotReloadContextStore?.appContext
  ?? createContext<AppContextType | null>(null);
if (hotReloadContextStore) {
  hotReloadContextStore.appContext = AppContext;
}

type ComposerContextActions = Pick<
  AppContextType,
  | "setStatus"
  | "sendMessage"
  | "compactSession"
  | "refreshGoal"
  | "updateGoal"
  | "clearGoal"
  | "interruptSession"
  | "switchAgent"
  | "switchModel"
  | "refreshAgents"
  | "setWorkspaceDefaultAgent"
  | "setWorkspaceDefaultProvider"
  | "switchContentView"
  | "createSession"
  | "renameSession"
  | "updateUiSettings"
>;

export interface ComposerContextType extends ComposerContextActions {
  state: ComposerStateSnapshot;
  getLatestAssistantContent: () => string | null;
}

export const ComposerContext = hotReloadContextStore?.composerContext
  ?? createContext<ComposerContextType | null>(null);
if (hotReloadContextStore) {
  hotReloadContextStore.composerContext = ComposerContext;
}

export function useAppState() {
  const ctx = useContext(AppContext);
  if (!ctx) {
    throw new Error("useAppState must be used within AppProvider");
  }
  return ctx;
}

export function useComposerState() {
  const ctx = useContext(ComposerContext);
  if (!ctx) {
    throw new Error("useComposerState must be used within AppProvider");
  }
  return ctx;
}
