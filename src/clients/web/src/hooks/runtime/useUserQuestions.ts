import { useCallback, useEffect, useRef, useState } from "react";
import { deleteUserQuestion, listUserQuestions, watchUserQuestions,
  type UserQuestionOwner, type UserQuestionPageDTO, type UserQuestionSummaryDTO } from "../../api/userQuestions";
import { errorMessage } from "../../utils/errorMessage";

/** 绑定发起 Tab 的精确 owner；折叠时仅观察轻量摘要，正文按需分页。 */
export function useUserQuestions(owner: UserQuestionOwner | undefined, expanded: boolean, report: (message: string) => void) {
  const [summary, setSummary] = useState<UserQuestionSummaryDTO>();
  const [page, setPage] = useState<UserQuestionPageDTO>();
  const [loading, setLoading] = useState(false);
  const [reload, setReload] = useState(0);
  const [removing, setRemoving] = useState<string>();
  const lifetime = useRef<AbortController>();
  const pageRequest = useRef<AbortController>();
  const currentPage = useRef(page);
  currentPage.current = page;
  const port = owner?.port, workspaceKey = owner?.workspaceKey, sessionId = owner?.sessionId, threadId = owner?.threadId;
  const acceptSummary = useCallback((value: UserQuestionSummaryDTO) => {
    setSummary(previous => !previous || value.revision >= previous.revision ? value : previous);
  }, []);
  useEffect(() => {
    setSummary(undefined); setPage(undefined); setRemoving(undefined);
    if (port === undefined || !workspaceKey || !sessionId || !threadId) return;
    const target = { port, workspaceKey, sessionId, threadId };
    const controller = new AbortController(); lifetime.current = controller;
    void watchUserQuestions(target, controller.signal, acceptSummary).catch(error => {
      if (!controller.signal.aborted) report(errorMessage(error));
    });
    return () => { controller.abort(); pageRequest.current?.abort(); };
  }, [port, workspaceKey, sessionId, threadId, acceptSummary, report]);

  useEffect(() => {
    const controller = new AbortController(); pageRequest.current = controller;
    if (!expanded || !owner || !summary?.count || summary.session_id !== sessionId || summary.thread_id !== threadId) { setPage(undefined); setLoading(false); return () => controller.abort(); }
    setLoading(true);
    void listUserQuestions(owner, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setPage(value); acceptSummary(value);
    }).catch(error => { if (!controller.signal.aborted) report(errorMessage(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
    // owner 的身份由四个稳定字段表达，不能因父组件重渲染重复请求。
  }, [port, workspaceKey, sessionId, threadId, expanded, summary?.revision, summary?.count, reload, acceptSummary, report]);

  const loadMore = async () => {
    const previous = currentPage.current, signal = pageRequest.current?.signal;
    if (!owner || !previous?.next_before || !signal || signal.aborted || loading) return;
    setLoading(true);
    try {
      const next = await listUserQuestions(owner, signal, previous.next_before);
      if (signal.aborted || currentPage.current !== previous) return;
      if (next.revision !== previous.revision) { acceptSummary(next); setReload(value => value + 1); return; }
      setPage({ ...next, items: [...previous.items, ...next.items] });
    } catch (error) { if (!signal.aborted) report(errorMessage(error)); }
    finally { if (!signal.aborted) setLoading(false); }
  };
  const remove = async (questionId: string) => {
    const signal = lifetime.current?.signal;
    if (!owner || !signal || signal.aborted || removing) return;
    setRemoving(questionId);
    try {
      const result = await deleteUserQuestion(owner, questionId, signal);
      if (!signal.aborted) acceptSummary(result);
    } catch (error) {
      if (!signal.aborted) { report(errorMessage(error)); setReload(value => value + 1); }
    } finally { if (!signal.aborted) setRemoving(undefined); }
  };
  return { count: summary?.session_id === sessionId && summary?.thread_id === threadId ? summary?.count ?? 0 : 0, items: page?.items ?? [], hasMore: Boolean(page?.next_before),
    loading, removing, loadMore, remove, retry: () => setReload(value => value + 1) };
}
