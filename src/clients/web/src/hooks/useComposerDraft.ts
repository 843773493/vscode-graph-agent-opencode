import { useCallback, useRef, useState, type Dispatch, type SetStateAction } from "react";
import {
  composerDraftScopeKey,
  readComposerDraft,
  writeComposerDraft,
} from "../state/composerDrafts/storage";
import { sessionScopeKey } from "../state/session/sessionScope";

const NEW_SESSION_DRAFT_ID = "__new_session_draft__";
const ephemeralDraftsByScope = new Map<string, string>();

function ephemeralDraftScopeKey(
  workspaceId: string | null,
  sessionId: string | null,
): string | null {
  if (!workspaceId) {
    return null;
  }
  return sessionScopeKey(workspaceId, sessionId ?? NEW_SESSION_DRAFT_ID);
}

export function useComposerDraft(
  workspaceId: string | null,
  sessionId: string | null,
  userScope?: string | null,
): [string, Dispatch<SetStateAction<string>>] {
  const persistentScopeKey = composerDraftScopeKey(workspaceId, sessionId, userScope);
  const ephemeralScopeKey = ephemeralDraftScopeKey(workspaceId, sessionId);
  const scopeKey = persistentScopeKey ?? ephemeralScopeKey;
  const activeScopeKeyRef = useRef(scopeKey);
  const activePersistentScopeKeyRef = useRef(persistentScopeKey);
  const [draftState, setDraftState] = useState(() => ({
    scopeKey,
    value: persistentScopeKey
      ? readComposerDraft(persistentScopeKey)
      : ephemeralScopeKey
        ? ephemeralDraftsByScope.get(ephemeralScopeKey) ?? ""
        : "",
  }));
  activeScopeKeyRef.current = scopeKey;
  activePersistentScopeKeyRef.current = persistentScopeKey;
  if (draftState.scopeKey !== scopeKey) {
    // React 会在提交 DOM 前重新渲染，避免先显示上一会话草稿再由 effect 修正。
    setDraftState({
      scopeKey,
      value: persistentScopeKey
        ? readComposerDraft(persistentScopeKey)
        : ephemeralScopeKey
          ? ephemeralDraftsByScope.get(ephemeralScopeKey) ?? ""
          : "",
    });
  }
  const draft = draftState.scopeKey === scopeKey
    ? draftState.value
    : persistentScopeKey
      ? readComposerDraft(persistentScopeKey)
      : ephemeralScopeKey
        ? ephemeralDraftsByScope.get(ephemeralScopeKey) ?? ""
        : "";

  const setDraft = useCallback<Dispatch<SetStateAction<string>>>((action) => {
    const scopeKeyAtCall = activeScopeKeyRef.current;
    const persistentScopeKeyAtCall = activePersistentScopeKeyRef.current;
    setDraftState((current) => {
      const currentValue = current.scopeKey === scopeKeyAtCall
        ? current.value
        : persistentScopeKeyAtCall
          ? readComposerDraft(persistentScopeKeyAtCall)
          : scopeKeyAtCall
            ? ephemeralDraftsByScope.get(scopeKeyAtCall) ?? ""
            : "";
      const next = typeof action === "function" ? action(currentValue) : action;
      if (persistentScopeKeyAtCall) {
        writeComposerDraft(persistentScopeKeyAtCall, next);
      } else if (scopeKeyAtCall) {
        if (next) {
          ephemeralDraftsByScope.set(scopeKeyAtCall, next);
        } else {
          ephemeralDraftsByScope.delete(scopeKeyAtCall);
        }
      }
      return { scopeKey: scopeKeyAtCall, value: next };
    });
  }, []);

  return [draft, setDraft];
}
