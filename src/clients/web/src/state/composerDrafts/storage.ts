import { sessionScopeKey } from "../session/sessionScope";
import { readBrowserView, updateBrowserView } from "../browserViewStore";

const MAX_COMPOSER_DRAFT_LENGTH = 100_000;
const NEW_SESSION_DRAFT_ID = "__new_session_draft__";

export function composerDraftScopeKey(
  workspaceId: string | null,
  sessionId: string | null,
  browserScope?: string | null,
  threadId?: string,
): string | null {
  if (!workspaceId) return null;
  const prefix = browserScope ? `${browserScope}::` : "";
  const sessionKey = `${prefix}${sessionScopeKey(workspaceId, sessionId ?? NEW_SESSION_DRAFT_ID)}`;
  return threadId === undefined ? sessionKey : `${sessionKey}::thread:${encodeURIComponent(threadId)}`;
}

export async function readComposerDraft(scopeKey: string | null): Promise<string> {
  return scopeKey ? await readBrowserView<string>(`draft:${scopeKey}`) ?? "" : "";
}

/** 超限时界面继续持有输入，同时由调用方明确提示尚未保存。 */
export function composerDraftExceedsLimit(content: string): boolean {
  return content.length > MAX_COMPOSER_DRAFT_LENGTH;
}

export async function writeComposerDraft(scopeKey: string | null, content: string): Promise<void> {
  if (!scopeKey) return;
  if (composerDraftExceedsLimit(content)) throw new Error(`Composer 草稿长度不能超过 ${MAX_COMPOSER_DRAFT_LENGTH} 个字符`);
  await updateBrowserView<string>(`draft:${scopeKey}`, () => content || undefined);
}

/** 一次性迁移旧 localStorage；保留完整原件，只有明确属于当前身份的草稿才移入本地视图。 */
export async function migrateComposerDrafts(gatewayId: string, userId: string | null): Promise<void> {
  const storageKey = "boxteam.web.composerDrafts";
  const raw = window.localStorage.getItem(storageKey);
  if (!raw) return;
  const drafts: Record<string, string> = JSON.parse(raw);
  if (Object.values(drafts).some(value => typeof value !== "string")) throw new Error("旧草稿格式无效，无法迁移到浏览器数据库");
  await updateBrowserView(`legacy-drafts:${gatewayId}`, current => current ?? drafts);
  const prefix = userId ? `user:${userId}::` : "";
  for (const [key, value] of Object.entries(drafts)) {
    if (key.startsWith("user:") && (!prefix || !key.startsWith(prefix))) continue;
    const suffix = prefix && key.startsWith(prefix) ? key.slice(prefix.length) : key;
    await updateBrowserView<string>(`draft:${gatewayId}::${suffix}`, current => current ?? value);
  }
  window.localStorage.removeItem(storageKey);
}
