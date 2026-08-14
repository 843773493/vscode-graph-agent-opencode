import { useCallback, useRef, useState, type Dispatch, type SetStateAction } from "react";
import {
  composerDraftScopeKey,
  readComposerDraft,
  writeComposerDraft,
} from "../state/composerDrafts/storage";

export function useComposerDraft(
  workspaceId: string | null,
  sessionId: string | null,
): [string, Dispatch<SetStateAction<string>>] {
  const scopeKey = composerDraftScopeKey(workspaceId, sessionId);
  const activeScopeKeyRef = useRef(scopeKey);
  const [draftState, setDraftState] = useState(() => ({
    scopeKey,
    value: readComposerDraft(scopeKey),
  }));
  activeScopeKeyRef.current = scopeKey;
  if (draftState.scopeKey !== scopeKey) {
    // React 会在提交 DOM 前重新渲染，避免先显示上一会话草稿再由 effect 修正。
    setDraftState({ scopeKey, value: readComposerDraft(scopeKey) });
  }
  const draft = draftState.scopeKey === scopeKey
    ? draftState.value
    : readComposerDraft(scopeKey);

  const setDraft = useCallback<Dispatch<SetStateAction<string>>>((action) => {
    setDraftState((current) => {
      const currentValue = current.scopeKey === activeScopeKeyRef.current
        ? current.value
        : readComposerDraft(activeScopeKeyRef.current);
      const next = typeof action === "function" ? action(currentValue) : action;
      writeComposerDraft(activeScopeKeyRef.current, next);
      return { scopeKey: activeScopeKeyRef.current, value: next };
    });
  }, []);

  return [draft, setDraft];
}
