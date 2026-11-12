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

function parseComposerDraft(value: unknown): string {
  if (value === undefined) return "";
  if (typeof value !== "string") throw new Error("Composer 草稿格式无法识别，原记录已保留，拒绝覆盖或删除");
  return value;
}

export async function readComposerDraft(scopeKey: string | null): Promise<string> {
  return scopeKey ? parseComposerDraft(await readBrowserView<unknown>(`draft:${scopeKey}`)) : "";
}

/** 超限时界面继续持有输入，同时由调用方明确提示尚未保存。 */
export function composerDraftExceedsLimit(content: string): boolean {
  return content.length > MAX_COMPOSER_DRAFT_LENGTH;
}

export async function writeComposerDraft(scopeKey: string | null, content: string): Promise<void> {
  if (!scopeKey) return;
  if (composerDraftExceedsLimit(content)) throw new Error(`Composer 草稿长度不能超过 ${MAX_COMPOSER_DRAFT_LENGTH} 个字符`);
  await updateBrowserView<unknown>(`draft:${scopeKey}`, current => {
    // 格式检查和修改在原事务内完成，读取后才出现的未知记录也不能被覆盖。
    parseComposerDraft(current);
    return content || undefined;
  });
}
