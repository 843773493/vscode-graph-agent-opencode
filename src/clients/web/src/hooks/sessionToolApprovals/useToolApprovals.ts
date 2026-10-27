import { useCallback, useEffect, useRef, useState } from "react";
import { getThreadToolApprovals, submitToolApprovals } from "../../api/jobs";
import type { ToolApproval, ToolApprovalCommand, ToolApprovalSubmitRequest } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";

export function useToolApprovals(port: number, workspaceId: string | null | undefined, sessionId: string, threadId: string) {
  const [approvals, setApprovals] = useState<ToolApproval[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<ToolApprovalCommand | null>(null);
  const lifetime = useRef<AbortController | null>(null);
  const submitting = useRef(false);
  const readVersion = useRef(0);
  const refresh = useCallback(async () => {
    const signal = lifetime.current?.signal;
    if (!signal || signal.aborted) return;
    const version = ++readVersion.current;
    const latest = await getThreadToolApprovals(port, workspaceId, sessionId, threadId, signal);
    if (!signal.aborted && version === readVersion.current) setApprovals(latest);
  }, [port, workspaceId, sessionId, threadId]);

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        await refresh();
      } catch (failure) {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 1500);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);

  const submit = useCallback(async (jobId: string, payload: ToolApprovalSubmitRequest) => {
    const signal = lifetime.current?.signal;
    if (!signal || signal.aborted || submitting.current) return;
    submitting.current = true;
    readVersion.current += 1;
    setBusy(true);
    setError(null);
    try {
      const accepted = await submitToolApprovals(port, workspaceId, jobId, payload, signal);
      if (!signal.aborted) setReceipt(accepted);
    } catch (failure) {
      if (!signal.aborted) setError(errorMessage(failure));
    } finally {
      try {
        await refresh();
      } catch (failure) {
        if (!signal.aborted) setError((previous) => [previous, errorMessage(failure)].filter(Boolean).join("；"));
      }
      submitting.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }, [port, workspaceId, refresh]);

  const retry = useCallback(async () => {
    try { await refresh(); setError(null); }
    catch (failure) { if (!lifetime.current?.signal.aborted) setError(errorMessage(failure)); }
  }, [refresh]);
  return { approvals, error, busy, receipt, submit, retry };
}
