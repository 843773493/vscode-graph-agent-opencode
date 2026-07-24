import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
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
  const [draft, setDraftState] = useState(() => readComposerDraft(scopeKey));

  useEffect(() => {
    activeScopeKeyRef.current = scopeKey;
    setDraftState(readComposerDraft(scopeKey));
  }, [scopeKey]);

  const setDraft = useCallback<Dispatch<SetStateAction<string>>>((action) => {
    setDraftState((current) => {
      const next = typeof action === "function" ? action(current) : action;
      writeComposerDraft(activeScopeKeyRef.current, next);
      return next;
    });
  }, []);

  return [draft, setDraft];
}
