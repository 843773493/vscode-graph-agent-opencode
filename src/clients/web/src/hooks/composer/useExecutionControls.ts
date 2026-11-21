import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { controlThreadExecution, getThreadExecutionControl, type ExecutionControlAction } from "../../api/jobs";
import type { ThreadExecutionControlDTO } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";
import { t } from "../../../../shared/core/i18n/index.js";

/** 控制镜像仅来自后端；每次挂载归属独立，迟到回执不得进入另一 Thread。 */
export function useExecutionControls({ apiPort, workspaceId, sessionId, threadId, activityKey, active }: {
  apiPort: number; workspaceId: string | null; sessionId: string | null; threadId: string | null;
  activityKey: string; active: boolean;
}) {
  const owner = useMemo(() => ({ apiPort, workspaceId, sessionId, threadId }), [apiPort, workspaceId, sessionId, threadId]);
  const ownerRef = useRef(owner); ownerRef.current = owner;
  const activeRef = useRef(active); activeRef.current = active;
  const [value, setValue] = useState<{
    owner: typeof owner; snapshot: ThreadExecutionControlDTO | null; pending: boolean; error: string;
  }>({ owner, snapshot: null, pending: false, error: "" });
  const controllerRef = useRef<AbortController | null>(null);
  const requestVersion = useRef(0);
  const pendingRef = useRef(false);
  const refresh = useCallback(async () => {
    const controller = controllerRef.current;
    if (!workspaceId || !sessionId || !threadId || !activeRef.current || pendingRef.current || ownerRef.current !== owner || !controller || controller.signal.aborted) return;
    const version = ++requestVersion.current;
    try {
      const snapshot = await getThreadExecutionControl(apiPort, workspaceId, sessionId, threadId, controller.signal);
      if (ownerRef.current === owner && !controller.signal.aborted && requestVersion.current === version) {
        setValue(previous => ({ owner, snapshot, pending: previous.owner === owner && previous.pending, error: "" }));
      }
    } catch (cause) {
      if (ownerRef.current === owner && !controller.signal.aborted && requestVersion.current === version) {
        setValue({ owner, snapshot: null, pending: false, error: errorMessage(cause) });
        throw cause;
      }
    }
  }, [apiPort, workspaceId, sessionId, threadId, owner]);
  const snapshot = value.owner === owner ? value.snapshot : null;
  const pending = value.owner === owner && value.pending;
  const error = value.owner === owner ? value.error : "";

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    pendingRef.current = false;
    return () => { controller.abort(); };
  }, [owner]);
  useEffect(() => {
    const confirm = () => {
      if (!active || globalThis.document?.visibilityState === "hidden") { ++requestVersion.current; return; }
      if (pendingRef.current) return;
      // refresh 已将当前归属的错误发布到控件。
      void refresh().catch(() => {});
    };
    confirm();
    globalThis.document?.addEventListener("visibilitychange", confirm);
    return () => globalThis.document?.removeEventListener("visibilitychange", confirm);
  }, [refresh, activityKey, active]);
  useEffect(() => {
    if (!active || pending || !snapshot && !error
      || snapshot?.state === "idle" && !snapshot.advancement_paused) return;
    // 可见时重试失败读取；失效快照不用于控制，后端确认空闲后停止轮询。
    const timer = globalThis.setTimeout(() => {
      if (globalThis.document?.visibilityState === "hidden") return;
      void refresh().catch(() => {});
    }, snapshot?.state === "pausing" ? 500 : 2000);
    return () => globalThis.clearTimeout(timer);
  }, [value, refresh, active]);

  const control = useCallback(async (action: ExecutionControlAction) => {
    const controller = controllerRef.current;
    if (!snapshot || !workspaceId || !activeRef.current || pendingRef.current || ownerRef.current !== owner || !controller || controller.signal.aborted) return;
    pendingRef.current = true;
    ++requestVersion.current;
    setValue({ owner, snapshot, pending: true, error: "" });
    try {
      const next = await controlThreadExecution(apiPort, workspaceId, snapshot, action, controller.signal);
      if (ownerRef.current === owner && !controller.signal.aborted) setValue({ owner, snapshot: next, pending: false, error: "" });
    } catch (cause) {
      let message = errorMessage(cause);
      if (ownerRef.current === owner) pendingRef.current = false;
      try { await refresh(); }
      catch (recoveryError) { message = t("composer.execution.recoveryFailed", message, errorMessage(recoveryError)); }
      if (ownerRef.current === owner && !controller.signal.aborted) {
        setValue(previous => ({ owner, snapshot: previous.owner === owner ? previous.snapshot : null, pending: false, error: message }));
      }
    } finally {
      if (ownerRef.current === owner) pendingRef.current = false;
    }
  }, [snapshot, workspaceId, owner, apiPort, refresh]);

  return { snapshot, pending, error, control, refresh };
}
