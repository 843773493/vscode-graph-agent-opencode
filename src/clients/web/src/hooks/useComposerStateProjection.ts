import { useCallback, useMemo, useRef, type MutableRefObject } from "react";
import type { AppState } from "../types/frontend";
import { sessionScopeKey } from "../state/session/sessionScope";
import {
  reuseComposerStateSnapshot,
  selectComposerState,
  type ComposerStateSnapshot,
} from "../state/composerState";
import type { ComposerContextType } from "../hooks";

/** Composer 状态投影入参：AppState 切片，加上 ComposerContext 需要的外部回调。 */
export interface ComposerStateProjectionInput
  extends Omit<ComposerContextType, "state" | "getLatestAssistantContent"> {
  state: AppState;
  currentSessionCacheKey: string | null;
  latestStateRef: MutableRefObject<AppState>;
}

export interface ComposerStateProjection {
  composerState: ComposerStateSnapshot;
  composerValue: ComposerContextType;
  getLatestAssistantContent: () => string | null;
}

/** Composer 状态投影链路：把 AppState 投影成 ComposerContext 订阅快照并复用其身份，
 * 同时提供读取最近一条非空助手正文的入口。 */
export function useComposerStateProjection({
  state,
  currentSessionCacheKey,
  latestStateRef,
  setStatus, sendMessage, compactSession, refreshGoal, updateGoal, clearGoal,
  interruptSession, switchAgent, switchModel, refreshAgents, switchContentView,
  setWorkspaceDefaultAgent, setWorkspaceDefaultProvider,
  createSession, renameSession, updateUiSettings,
}: ComposerStateProjectionInput): ComposerStateProjection {
  const composerStateRef = useRef<ComposerStateSnapshot | null>(null);
  const selectedComposerState = selectComposerState(state, currentSessionCacheKey);
  const composerState = reuseComposerStateSnapshot(
    composerStateRef.current,
    selectedComposerState,
  );
  // 渲染期写入而非 effect：渲染被丢弃时本次投影仍要参与下次快照身份复用。
  composerStateRef.current = composerState;

  const getLatestAssistantContent = useCallback((): string | null => {
    const latest = latestStateRef.current;
    const latestSessionId = latest.currentSession?.session_id ?? null;
    const latestWorkspaceId =
      latest.currentSessionWorkspaceId ?? latest.activeGatewayWorkspaceId;
    const scopeKey = latestSessionId && latestWorkspaceId
      ? sessionScopeKey(latestWorkspaceId, latestSessionId)
      : latestSessionId;
    const timeline = scopeKey
      ? latest.turnTimelinesBySession.get(scopeKey)
      : null;
    if (timeline) {
      for (let index = timeline.orderedTurnIds.length - 1; index >= 0; index -= 1) {
        const turn = timeline.turnsById[timeline.orderedTurnIds[index]];
        if (!turn) {
          continue;
        }
        const content = "final_response" in turn
          ? turn.final_response ?? turn.response_preview ?? ""
          : turn.response_preview ?? "";
        if (content.trim()) {
          return content;
        }
      }
    }
    return null;
  }, []);

  const composerValue = useMemo<ComposerContextType>(() => ({
    state: composerState,
    getLatestAssistantContent,
    setStatus, sendMessage, compactSession, refreshGoal, updateGoal, clearGoal,
    interruptSession, switchAgent, switchModel, refreshAgents, switchContentView,
    setWorkspaceDefaultAgent, setWorkspaceDefaultProvider,
    createSession, renameSession, updateUiSettings,
  }), [
    composerState, getLatestAssistantContent, setStatus, sendMessage,
    compactSession, refreshGoal, updateGoal, clearGoal, interruptSession,
    switchAgent, switchModel, refreshAgents, switchContentView, createSession,
    setWorkspaceDefaultAgent, setWorkspaceDefaultProvider, renameSession,
    updateUiSettings,
  ]);

  return { composerState, composerValue, getLatestAssistantContent };
}
