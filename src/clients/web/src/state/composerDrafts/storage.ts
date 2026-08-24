import { sessionScopeKey } from "../session/sessionScope";

const COMPOSER_DRAFTS_STORAGE_KEY = "boxteam.web.composerDrafts";
const MAX_COMPOSER_DRAFTS = 100;
const MAX_COMPOSER_DRAFT_LENGTH = 100_000;
const NEW_SESSION_DRAFT_ID = "__new_session_draft__";

export function composerDraftScopeKey(
  workspaceId: string | null,
  sessionId: string | null,
  userScope?: string | null,
): string | null {
  if (!workspaceId) {
    return null;
  }
  if (userScope === null) {
    return null;
  }
  const scopePrefix = userScope ? `${userScope}::` : "";
  return `${scopePrefix}${sessionScopeKey(workspaceId, sessionId ?? NEW_SESSION_DRAFT_ID)}`;
}

function readComposerDrafts(): Record<string, string> {
  if (typeof window === "undefined") {
    return {};
  }
  const raw = window.localStorage.getItem(COMPOSER_DRAFTS_STORAGE_KEY);
  return raw ? JSON.parse(raw) as Record<string, string> : {};
}

export function readComposerDraft(scopeKey: string | null): string {
  return scopeKey ? readComposerDrafts()[scopeKey] ?? "" : "";
}

export function writeComposerDraft(scopeKey: string | null, content: string): void {
  if (typeof window === "undefined" || !scopeKey) {
    return;
  }
  if (content.length > MAX_COMPOSER_DRAFT_LENGTH) {
    throw new Error(`Composer 草稿长度不能超过 ${MAX_COMPOSER_DRAFT_LENGTH} 个字符`);
  }
  const drafts = readComposerDrafts();
  if (content) {
    delete drafts[scopeKey];
    drafts[scopeKey] = content;
  } else {
    delete drafts[scopeKey];
  }
  const keys = Object.keys(drafts);
  for (const staleKey of keys.slice(0, Math.max(0, keys.length - MAX_COMPOSER_DRAFTS))) {
    delete drafts[staleKey];
  }
  window.localStorage.setItem(COMPOSER_DRAFTS_STORAGE_KEY, JSON.stringify(drafts));
}
