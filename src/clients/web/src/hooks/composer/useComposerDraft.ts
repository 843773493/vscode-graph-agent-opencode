import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { composerDraftScopeKey, composerDraftExceedsLimit, readComposerDraft, writeComposerDraft } from "../../state/composerDrafts/storage";
import { readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import type { SelectedAttachment } from "../../utils/media/mediaAttachments";
import { errorMessage } from "../../utils/errorMessage";

const EMPTY_ATTACHMENTS: SelectedAttachment[] = [];

function useStoredDraft<T>(scopeKey: string | null, empty: T, read: (key: string | null) => Promise<T>, write: (key: string | null, value: T) => Promise<void>, onError?: (message: string) => void): [T, Dispatch<SetStateAction<T>>] {
  const [draft, setDraft] = useState({ scopeKey, value: empty });
  const current = useRef(draft);
  const report = useRef(onError); report.current = onError;
  const [failure, setFailure] = useState<Error | null>(null);
  const reportFailure = useCallback((error: unknown) => {
    const message = `草稿尚未保存：${errorMessage(error)}`;
    if (report.current) report.current(message);
    else setFailure(new Error(message));
  }, []);
  if (current.current.scopeKey !== scopeKey) {
    current.current = { scopeKey, value: empty };
    setDraft(current.current);
  }
  useEffect(() => {
    let active = true;
    const before = current.current;
    void read(scopeKey).then(value => {
      // 恢复期间的新输入和 owner 切换优先，旧读取不能覆盖它们。
      if (!active || current.current !== before) return;
      current.current = { scopeKey, value };
      setDraft(current.current);
    }).catch(error => { if (active) reportFailure(error); });
    return () => { active = false; };
  }, [scopeKey, read, reportFailure]);
  const update = useCallback<Dispatch<SetStateAction<T>>>(action => {
    const before = current.current;
    const value = typeof action === "function" ? (action as (current: T) => T)(before.value) : action;
    current.current = { scopeKey: before.scopeKey, value };
    setDraft(current.current);
    void write(before.scopeKey, value).catch(reportFailure);
  }, [write, reportFailure]);
  if (failure) throw failure;
  return [draft.scopeKey === scopeKey ? draft.value : empty, update];
}

async function saveText(key: string | null, value: string): Promise<void> {
  if (composerDraftExceedsLimit(value)) throw new Error("输入超过 100000 个字符，当前内容仅保留在输入框中");
  await writeComposerDraft(key, value);
}

export function useComposerDraft(workspaceId: string | null, sessionId: string | null, browserScope?: string | null, threadId?: string, onError?: (message: string) => void): [string, Dispatch<SetStateAction<string>>] {
  return useStoredDraft(composerDraftScopeKey(workspaceId, sessionId, browserScope, threadId), "", readComposerDraft, saveText, onError);
}

async function readAttachments(key: string | null): Promise<SelectedAttachment[]> {
  return key ? await readBrowserView<SelectedAttachment[]>(`attachments:${key}`) ?? EMPTY_ATTACHMENTS : EMPTY_ATTACHMENTS;
}
export async function writeComposerAttachments(key: string | null, attachments: SelectedAttachment[]): Promise<void> {
  if (key) await updateBrowserView(`attachments:${key}`, () => attachments.length ? attachments : undefined);
}

/** 附件 data URL 与引用由 IndexedDB 保存，不再随输入框卸载而丢失。 */
export function useComposerAttachments(key: string | null, onError: (message: string) => void): [SelectedAttachment[], Dispatch<SetStateAction<SelectedAttachment[]>>] {
  return useStoredDraft(key, EMPTY_ATTACHMENTS, readAttachments, writeComposerAttachments, onError);
}
