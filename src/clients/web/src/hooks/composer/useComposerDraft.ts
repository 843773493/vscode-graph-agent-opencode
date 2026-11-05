import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { composerDraftScopeKey, composerDraftExceedsLimit, readComposerDraft, writeComposerDraft } from "../../state/composerDrafts/storage";
import { readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import type { SelectedAttachment } from "../../utils/media/mediaAttachments";
import { errorMessage } from "../../utils/errorMessage";

const EMPTY_ATTACHMENTS: SelectedAttachment[] = [];

export interface ComposerDraftController {
  append(text: string): Promise<void>;
}

type DraftEdit<T> = (action: SetStateAction<T>) => Promise<void>;

function useStoredDraft<T>(scopeKey: string | null, empty: T, read: (key: string | null) => Promise<T>, write: (key: string | null, value: T) => Promise<void>, onError?: (message: string) => void): [T, Dispatch<SetStateAction<T>>, DraftEdit<T> | undefined] {
  const scope = useMemo(() => ({ key: scopeKey, active: false }), [scopeKey]);
  const [draft, setDraft] = useState({ scope, value: empty, ready: false });
  const current = useRef(draft);
  const report = useRef(onError); report.current = onError;
  const [failure, setFailure] = useState<Error | null>(null);
  const reportFailure = useCallback((error: unknown) => {
    const message = `草稿尚未保存：${errorMessage(error)}`;
    if (report.current) report.current(message);
    else setFailure(new Error(message));
  }, []);
  if (current.current.scope !== scope) {
    current.current = { scope, value: empty, ready: false };
    setDraft(current.current);
  }
  useEffect(() => {
    let active = true;
    scope.active = true;
    const before = current.current;
    void read(scope.key).then(value => {
      // 恢复期间的新输入和 owner 切换优先，旧读取不能覆盖它们。
      if (!active || current.current !== before) return;
      current.current = { scope, value, ready: true };
      setDraft(current.current);
    }).catch(error => { if (active) reportFailure(error); });
    return () => { active = false; scope.active = false; };
  }, [scope, read, reportFailure]);
  const apply = useCallback(async (action: SetStateAction<T>, requireReady: boolean) => {
    const before = current.current;
    if (!scope.active || before.scope !== scope) throw new Error("接收草稿已关闭或改变，请重新选择 Thread");
    if (requireReady && !before.ready) throw new Error("接收草稿尚未恢复，请稍后重试");
    const value = typeof action === "function" ? (action as (current: T) => T)(before.value) : action;
    current.current = { scope, value, ready: true };
    setDraft(current.current);
    await write(scope.key, value);
  }, [scope, write]);
  const reportScopeFailure = useCallback((error: unknown) => {
    if (scope.active && current.current.scope === scope) reportFailure(error);
  }, [scope, reportFailure]);
  const update = useCallback<Dispatch<SetStateAction<T>>>(action => { void apply(action, false).catch(reportScopeFailure); }, [apply, reportScopeFailure]);
  const edit = useCallback<DraftEdit<T>>(action => apply(action, true).catch(error => { reportScopeFailure(error); throw error; }), [apply, reportScopeFailure]);
  if (failure) throw failure;
  return [draft.scope === scope ? draft.value : empty, update, draft.scope === scope && draft.ready ? edit : undefined];
}

async function saveText(key: string | null, value: string): Promise<void> {
  if (composerDraftExceedsLimit(value)) throw new Error("输入超过 100000 个字符，当前内容仅保留在输入框中");
  await writeComposerDraft(key, value);
}

export function useComposerDraft(workspaceId: string | null, sessionId: string | null, browserScope?: string | null, threadId?: string, onError?: (message: string) => void): [string, Dispatch<SetStateAction<string>>, ComposerDraftController | undefined] {
  const [value, update, edit] = useStoredDraft(composerDraftScopeKey(workspaceId, sessionId, browserScope, threadId), "", readComposerDraft, saveText, onError);
  const controller = useMemo(() => edit ? { append: (text: string) => edit(current => current ? `${current}\n${text}` : text) } : undefined, [edit]);
  return [value, update, controller];
}

async function readAttachments(key: string | null): Promise<SelectedAttachment[]> {
  return key ? await readBrowserView<SelectedAttachment[]>(`attachments:${key}`) ?? EMPTY_ATTACHMENTS : EMPTY_ATTACHMENTS;
}
export async function writeComposerAttachments(key: string | null, attachments: SelectedAttachment[]): Promise<void> {
  if (key) await updateBrowserView(`attachments:${key}`, () => attachments.length ? attachments : undefined);
}

/** 附件 data URL 与引用由 IndexedDB 保存，不再随输入框卸载而丢失。 */
export function useComposerAttachments(key: string | null, onError: (message: string) => void): [SelectedAttachment[], Dispatch<SetStateAction<SelectedAttachment[]>>] {
  const [value, update] = useStoredDraft(key, EMPTY_ATTACHMENTS, readAttachments, writeComposerAttachments, onError);
  return [value, update];
}
