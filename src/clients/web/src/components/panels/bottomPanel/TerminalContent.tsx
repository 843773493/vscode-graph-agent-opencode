import { failureSummary } from "../../../../../shared/core/failurePresentation";
import type { PanelToolbarControl, RenderPanelToolbar } from "./PanelToolbar";
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import type { WorkspaceResourcesController } from "../../../hooks/workspace/useWorkspaceResources";
import type { ResourceAction, ResourceThreadRef, WorkspaceResourceDTO } from "../../../api/workspaceResources";
import { TerminalOutput, TerminalThemeContext } from "./TerminalOutput";
import { buildGatewayAttachUrl } from "../../../utils/attachUrls";
import { errorMessage } from "../../../utils/errorMessage";
import { statusLabel, stripTerminalNamePrefix } from "../../../state/display/resourceDisplay";
import { formatLocalTime } from "../../../utils/format";
import { useTerminalFrameObservation } from "../../../hooks/workspace/useTerminalFrameObservation";

/** 终端 iframe 等待就绪的上限；超过即视为无响应，必须给用户明确的兜底说明与重试入口。 */
export const TERMINAL_FRAME_READY_TIMEOUT_MS = 15_000;

type TerminalFrameStatus = "loading" | "ready" | "stalled";
const isStoppedTerminal = (status: string) => ["exited", "terminated", "deleted"].includes(status);

interface TerminalContentProps {
  apiPort: number;
  controller: WorkspaceResourcesController;
  currentThread: ResourceThreadRef | null;
  threadTargetUnavailable: boolean;
  workspaceId: string | null;
  workspaceName: string;
  selectedTerminalId: string | null;
  onSelectTerminal: (terminalId: string | null) => void;
  renderToolbar: RenderPanelToolbar;
  showTerminalMenu: (anchor: { x: number; y: number }, actions: readonly Extract<PanelToolbarControl, { kind: "action" }>[]) => void;
}

/** 终端产品内容；标签、停靠和关闭由所在工作台提供。 */
export function TerminalContent({
  apiPort, controller, currentThread, threadTargetUnavailable, workspaceId, workspaceName,
  selectedTerminalId, onSelectTerminal, renderToolbar, showTerminalMenu,
}: TerminalContentProps) {
  const { items: entries, loading } = controller;
  const [outputReloadToken, setOutputReloadToken] = useState(0);
  const onRefresh = () => {
    setOutputReloadToken((token) => token + 1);
    void controller.refresh();
  };
  const [retentionScope, setRetentionScope] = useState<"thread" | "workspace">("thread");
  const isMutating = controller.mutating;
  const [terminalListWidth, setTerminalListWidth] = useState<number | null>(null);
  const [frameStatus, setFrameStatus] = useState<TerminalFrameStatus>("loading");
  const [frameReloadToken, setFrameReloadToken] = useState(0);
  const resizingRef = useRef(false);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const terminalTheme = useContext(TerminalThemeContext);
  const selectedEntry = useMemo(
    () => selectedTerminalId === null ? entries[0] ?? null : entries.find(
      (entry) => entry.resource_id === selectedTerminalId,
    ) ?? null,
    [entries, selectedTerminalId],
  );
  const selectedIdRef = useRef(selectedTerminalId);
  selectedIdRef.current = selectedTerminalId;
  const resourceWarning = selectedEntry?.reconciliation_status !== "verified" ? selectedEntry?.error : null;
  const terminalErrorMessage = [resourceWarning ? null : selectedEntry?.error, controller.error]
    .filter((message): message is string => typeof message === "string" && message.length > 0)
    .join(" · ");
  const selectedIsStopped = selectedEntry ? isStoppedTerminal(selectedEntry.status) : false;
  const isCurrentThreadAssociated = selectedEntry?.thread_associations.some((ref) => ref.session_id === currentThread?.session_id && ref.thread_id === currentThread?.thread_id);
  const canAttachToCurrentThread = currentThread && !isCurrentThreadAssociated && (selectedEntry?.retention_scope === "workspace"
    || (selectedEntry?.source_thread?.session_id === currentThread.session_id && selectedEntry?.source_thread?.thread_id === currentThread.thread_id));
  const canControlTerminal = (resource: WorkspaceResourceDTO | null, action: ResourceAction) =>
    !isMutating && !threadTargetUnavailable && Boolean(resource?.available_actions.includes(action));
  // 终端服务未就绪时 iframe 只会长时间空白，不会触发 onError；因此必须自行计时，
  // 把「一直转圈」变成明确的「超时 + 重试」。URL 构造失败同样不得在 render 阶段抛出。
  const attach = useMemo((): { url: string } | { error: string } | null => {
    if (!selectedEntry) return null;
    if (threadTargetUnavailable) return { error: t("bottom.terminal.attach.threadTargetUnavailable") };
    if (isStoppedTerminal(selectedEntry.status)) return { error: t("bottom.terminal.attach.stopped") };
    if (selectedEntry.reconciliation_status !== "verified") return { error: t("bottom.terminal.attach.unverified") };
    if (selectedEntry.status !== "running") return { error: t("bottom.terminal.attach.stopped") };
    if (selectedEntry.retention_scope === "thread" && selectedEntry.thread_associations.length === 0) {
      return { error: t("bottom.terminal.attach.noAssociation") };
    }
    try {
      return {
        url: buildGatewayAttachUrl(
          "terminal",
          workspaceId ?? "",
          selectedEntry.resource_id,
          true,
          selectedEntry.retention_scope === "thread" ? selectedEntry.source_thread : undefined,
        ),
      };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  }, [selectedEntry?.resource_id, selectedEntry?.owner_revision, selectedEntry?.status, selectedEntry?.reconciliation_status, selectedEntry?.retention_scope, selectedEntry?.source_thread, selectedEntry?.thread_associations, threadTargetUnavailable, workspaceId]);

  const frameUrl = attach && "url" in attach ? attach.url : null;
  useTerminalFrameObservation(frameRef, frameUrl);
  // 观察补读会替换完整资源对象；就绪期限只随实际 iframe 地址或显式重试重新开始。
  useEffect(() => {
    if (!frameUrl) {
      setFrameStatus("stalled");
      return;
    }
    setFrameStatus("loading");
    const timerId = window.setTimeout(() => {
      setFrameStatus((current) => (current === "loading" ? "stalled" : current));
    }, TERMINAL_FRAME_READY_TIMEOUT_MS);
    const terminalId = new URL(frameUrl).searchParams.get("terminalId");
    const receiveReady = (event: MessageEvent<unknown>) => {
      const source = frameRef.current?.contentWindow;
      if (!source || event.origin !== window.location.origin || event.source !== source
        || !event.data || typeof event.data !== "object") return;
      const message = event.data as { type?: string; terminalId?: string };
      if (message.type !== "boxteam.terminal.ready" || message.terminalId !== terminalId) return;
      window.clearTimeout(timerId);
      setFrameStatus("ready");
    };
    window.addEventListener("message", receiveReady);
    return () => { window.clearTimeout(timerId); window.removeEventListener("message", receiveReady); };
  }, [frameUrl, frameReloadToken]);

  useEffect(() => {
    if (frameStatus === "ready") frameRef.current?.contentWindow?.postMessage({
      type: "boxteam.terminal.theme", terminalId: selectedEntry?.resource_id, theme: terminalTheme,
    }, window.location.origin);
  }, [terminalTheme, frameStatus, frameUrl, selectedEntry?.resource_id]);

  const controlTerminal = async (resource: WorkspaceResourceDTO | null, action: ResourceAction, thread?: ResourceThreadRef) => {
    if (!resource || !canControlTerminal(resource, action)) return;
    try { return await controller.control(resource, action, thread); }
    catch { /* controller 已显示 owner 原始错误并重取完整资源。 */ }
  };
  const removeTerminal = async (resource: WorkspaceResourceDTO, action: "stop" | "delete") => {
    const result = await controlTerminal(resource, action);
    // 只在明确删除当前项后选择相邻项；等待期间用户换到其它终端时保留其选择。
    if (result?.status === "deleted" && selectedIdRef.current === resource.resource_id) {
      const index = entries.findIndex(entry => entry.resource_id === resource.resource_id);
      onSelectTerminal(entries[index + 1]?.resource_id ?? entries[index - 1]?.resource_id ?? null);
    }
  };
  const createTerminal = async (scope = retentionScope) => {
    if (!currentThread || threadTargetUnavailable) return;
    try { onSelectTerminal((await controller.create(currentThread, scope)).resource_id); }
    catch { /* controller 已显示创建错误并重新读取工作区清单。 */ }
  };

  const openTerminalMenu = (entry: WorkspaceResourceDTO, anchor: { x: number; y: number }) => {
    const actions: Extract<PanelToolbarControl, { kind: "action" }>[] = [];
    // 传输动作只操作当前显示的 iframe；后台终端的管理始终交原 owner。
    if (selectedEntry?.resource_id === entry.resource_id && frameUrl) {
      for (const command of ["connect", "disconnect", "search", "refresh"] as const) {
        actions.push({ id: command, kind: "action", label: t(command === "refresh" ? "bottom.terminal.refreshSnapshot" : `bottom.terminal.${command}`),
          disabled: frameStatus !== "ready", run: () => {
            const frame = frameRef.current;
            if (!frame?.contentWindow || new URL(frame.src).searchParams.get("terminalId") !== entry.resource_id) {
              throw new Error(t("workbench.resource.sourceMismatch"));
            }
            frame.contentWindow.postMessage({ type: "boxteam.terminal.action", terminalId: entry.resource_id, action: command }, window.location.origin);
          } });
      }
    } else actions.push({ id: "refresh", kind: "action", label: t("bottom.terminal.refresh"), run: onRefresh });
    if (entry.retention_scope === "thread") actions.push({ id: "keep", kind: "action", label: t("bottom.terminal.keep"),
      disabled: !canControlTerminal(entry, "promote"), run: () => controlTerminal(entry, "promote") });
    actions.push({ id: "attach", kind: "action", label: t("resources.attachThread"),
      disabled: !canControlTerminal(entry, "attach"), run: () => controlTerminal(entry, "attach") });
    actions.push({ id: "detach", kind: "action", label: t("bottom.terminal.detach"),
      disabled: !canControlTerminal(entry, "detach") || entry.thread_associations.length === 0,
      run: () => controlTerminal(entry, "detach") });
    actions.push({ id: "stop", kind: "action", label: t("bottom.terminal.stop"),
      disabled: !canControlTerminal(entry, "stop"), run: () => removeTerminal(entry, "stop") },
    { id: "delete", kind: "action", label: t("bottom.terminal.delete"),
      disabled: !canControlTerminal(entry, "delete") || entry.thread_associations.length > 0, run: () => removeTerminal(entry, "delete") });
    showTerminalMenu(anchor, actions);
  };

  const startListResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    resizingRef.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const resizeTerminalList = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!resizingRef.current) {
      return;
    }
    const bounds = event.currentTarget.parentElement!.getBoundingClientRect();
    const nextWidth = Math.min(460, bounds.width - 127, Math.max(40, bounds.right - event.clientX));
    setTerminalListWidth(nextWidth);
  };

  const stopListResize = (event: React.PointerEvent<HTMLDivElement>) => {
    resizingRef.current = false;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  return (
    <>
      {renderToolbar([
        { id: 'terminal', kind: 'select', primary: true, label: t("bottom.terminal.list"), value: selectedEntry?.resource_id ?? '',
          options: entries.map(entry => ({ value: entry.resource_id, label: `${stripTerminalNamePrefix(String(entry.metadata?.title ?? entry.resource_id))} · ${statusLabel(entry.status)}` })),
          disabled: entries.length === 0, onChange: onSelectTerminal },
        { id: 'create', kind: 'action', primary: true, label: t("bottom.terminal.create"), icon: 'add',
          disabled: isMutating || !currentThread || threadTargetUnavailable, run: () => createTerminal(),
          alternatives: [{ label: t("bottom.terminal.temporary"), run: () => createTerminal('thread') },
            { label: t("resources.keep"), run: () => createTerminal('workspace') }] },
        { id: 'refresh', kind: 'action', label: t("bottom.terminal.refresh"), icon: 'refresh', disabled: loading, run: onRefresh },
        { id: 'retention', kind: 'select', label: t("bottom.terminal.retentionScope"), icon: 'pin', value: retentionScope,
          options: [{ value: 'thread', label: t("bottom.terminal.temporary") }, { value: 'workspace', label: t("resources.keep") }],
          onChange: value => setRetentionScope(value as 'thread' | 'workspace') },
        ...(selectedEntry ? [
          ...(selectedEntry.retention_scope === 'thread' ? [{ id: 'keep', kind: 'action' as const, label: t("bottom.terminal.keep"), icon: 'pinned',
            disabled: !canControlTerminal(selectedEntry, 'promote'), run: () => controlTerminal(selectedEntry, 'promote') }] : []),
          { id: 'attach', kind: 'action' as const, label: isCurrentThreadAssociated ? t("bottom.terminal.alreadyAssociated") : t("bottom.terminal.associate"), icon: 'link',
            disabled: !canControlTerminal(selectedEntry, 'attach') || !canAttachToCurrentThread,
            run: () => currentThread ? controlTerminal(selectedEntry, 'attach', currentThread) : undefined },
          { id: 'stop', kind: 'action' as const, primary: true, label: t("bottom.terminal.stop"), icon: 'trash',
            disabled: !canControlTerminal(selectedEntry, 'stop'), run: () => controlTerminal(selectedEntry, 'stop') },
          { id: 'delete', kind: 'action' as const, label: t("bottom.terminal.delete"), icon: 'trash',
            disabled: !canControlTerminal(selectedEntry, 'delete') || selectedEntry.thread_associations.length > 0, run: () => removeTerminal(selectedEntry, 'delete') },
          ...selectedEntry.thread_associations.map(ref => ({ id: `detach-${ref.session_id}-${ref.thread_id}`, kind: 'action' as const,
            label: `${t("bottom.terminal.detach")} · ${ref.session_id} / ${ref.thread_id}`, icon: 'unlink',
            disabled: !canControlTerminal(selectedEntry, 'detach'), run: () => controlTerminal(selectedEntry, 'detach', ref) })),
        ] : []),
      ])}

      <div className="terminal-panel-content">
        <article className="terminal-panel-viewer" aria-label={t("bottom.terminal.content")}>
          <div className="terminal-panel-error-slot">
            {resourceWarning ? (
              <details key={selectedEntry!.resource_id} className="terminal-panel-resource-warning">
                <summary>{t(selectedIsStopped ? "bottom.terminal.recovery.history" : "bottom.terminal.recovery.unavailable")}</summary>
                <div>{resourceWarning}</div>
              </details>
            ) : null}
            {terminalErrorMessage ? (
              <div className="failure-summary" role="alert">
                {failureSummary(terminalErrorMessage)}
              </div>
            ) : null}
          </div>
          {selectedEntry && selectedIsStopped && workspaceId ? (
              <TerminalOutput
                key={`${workspaceId}:${selectedEntry.resource_id}:${selectedEntry.owner_revision}:${outputReloadToken}`}
                apiPort={apiPort} workspaceId={workspaceId} resourceId={selectedEntry.resource_id}
              />
          ) : selectedEntry ? (
            <div className="terminal-panel-frame-host">
              {attach && "url" in attach ? (
                <iframe
                  ref={frameRef}
                  key={`${attach.url}#${frameReloadToken}`}
                  src={attach.url}
                  className="terminal-panel-frame"
                  title={`${String(selectedEntry.metadata?.title ?? selectedEntry.resource_id)} · ${selectedEntry.retention_scope === "workspace" ? t("resources.keep") : t("bottom.terminal.temporary")}`}
                  onError={() => setFrameStatus("stalled")}
                />
              ) : null}
              {frameStatus === "stalled" ? (
                <div className="terminal-panel-frame-fallback" role="alert">
                  <strong>
                    {attach && "error" in attach
                      ? t("bottom.terminal.attach.unavailable")
                      : t("bottom.terminal.attach.timeout")}
                  </strong>
                  <span>
                    {attach && "error" in attach
                      ? attach.error
                      : t("bottom.terminal.attach.timeoutDetail")}
                  </span>
                  {attach && "error" in attach && workspaceId ? <TerminalOutput
                    key={`${workspaceId}:${selectedEntry.resource_id}:${selectedEntry.owner_revision}:${outputReloadToken}`}
                    apiPort={apiPort} workspaceId={workspaceId} resourceId={selectedEntry.resource_id}
                  /> : null}
                  {attach && "error" in attach ? null : (
                    <button
                      type="button"
                      onClick={() => setFrameReloadToken((token) => token + 1)}
                    >
                      {t("bottom.terminal.retryConnection")}
                    </button>
                  )}
                </div>
              ) : null}
            </div>
          ) : (
            <div className="terminal-panel-viewer-empty">
              {selectedTerminalId && !loading ? <strong role="alert">{t("bottom.terminal.selectedMissing", selectedTerminalId)}</strong>
                : <strong>{loading ? t("bottom.terminal.loading") : t("bottom.terminal.empty")}</strong>}
            </div>
          )}
        </article>

        <div
          className="terminal-panel-sash"
          role="separator"
          aria-label={t("bottom.terminal.resizeList")}
          aria-orientation="vertical"
          tabIndex={0}
          onPointerDown={startListResize}
          onPointerMove={resizeTerminalList}
          onPointerUp={stopListResize}
          onPointerCancel={stopListResize}
          onKeyDown={event => {
            if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
            event.preventDefault();
            const contentWidth = event.currentTarget.parentElement!.clientWidth;
            setTerminalListWidth(width => Math.min(460, contentWidth - 127, Math.max(40, (width ?? 180) + (event.key === 'ArrowLeft' ? 20 : -20))));
          }}
        />

        <aside
          className="terminal-panel-list"
          aria-label={`${t("bottom.terminal.list")} · ${workspaceName} · ${t("bottom.terminal.count", entries.length)}`}
          style={{ flex: `0 1 ${terminalListWidth ?? 180}px` }}
        >
          <div className="terminal-panel-list-scroll">
            {entries.map((entry) => {
              const action = isStoppedTerminal(entry.status) ? "delete" : "stop";
              const label = t(action === "delete" ? "bottom.terminal.deleteTitle" : "resources.action.terminate");
              const associated = action === "delete" && entry.thread_associations.length > 0;
              const disabled = associated || !canControlTerminal(entry, action);
              const reason = associated ? t("bottom.terminal.detachBeforeDelete")
                : isMutating ? t("resources.errors.operationInProgress")
                : threadTargetUnavailable ? t("bottom.terminal.attach.threadTargetUnavailable")
                : disabled ? entry.error ?? t("workbench.resource.actionUnavailable", label) : label;
              return <div key={entry.resource_id} data-resource-id={entry.resource_id}
                className={`terminal-panel-list-row${entry.resource_id === selectedEntry?.resource_id ? " active" : ""}`}
                onContextMenu={event => { event.preventDefault(); openTerminalMenu(entry, { x: event.clientX, y: event.clientY }); }}>
              <button
                type="button"
                className={`terminal-panel-list-item${entry.resource_id === selectedEntry?.resource_id ? " active" : ""}`}
                aria-pressed={entry.resource_id === selectedEntry?.resource_id}
                onClick={() => onSelectTerminal(entry.resource_id)}
                onKeyDown={event => {
                  if (event.key !== "Delete" || disabled) return;
                  event.preventDefault();
                  void removeTerminal(entry, action);
                }}
                title={[String(entry.metadata?.title ?? entry.resource_id), statusLabel(entry.status),
                  entry.retention_scope === "workspace" ? t("resources.keep") : t("bottom.terminal.temporary"),
                  ...(typeof entry.metadata?.exit_code === "number" ? [`${t("resources.metadata.exitCode")} ${entry.metadata.exit_code}`] : []),
                  ...entry.thread_associations.map(ref => `${ref.session_id} / ${ref.thread_id}`),
                  ...(entry.reclaim_at ? [t("bottom.terminal.reclaimAt", formatLocalTime(entry.reclaim_at))] : []),
                ].join(' · ')}
              >
                <span className={`codicon codicon-${isStoppedTerminal(entry.status) ? "debug-stop" : "terminal"}`} aria-hidden="true" />
                <span className="terminal-panel-list-copy">
                  <span className="terminal-panel-list-name">
                    {stripTerminalNamePrefix(String(entry.metadata?.title ?? entry.resource_id))}
                  </span>
                  <span className="terminal-panel-list-status">{statusLabel(entry.status)}</span>
                </span>
              </button>
              <button type="button" className="terminal-panel-list-action terminal-panel-list-more"
                aria-label={`${t("bottom.terminal.more")} · ${stripTerminalNamePrefix(String(entry.metadata?.title ?? entry.resource_id))}`}
                title={t("bottom.terminal.more")} aria-haspopup="menu" onClick={event => {
                  const bounds = event.currentTarget.getBoundingClientRect();
                  openTerminalMenu(entry, { x: bounds.left, y: bounds.bottom });
                }}>
                <span className="codicon codicon-ellipsis" aria-hidden="true" />
              </button>
              <button type="button" className="terminal-panel-list-action"
                aria-label={`${label} · ${stripTerminalNamePrefix(String(entry.metadata?.title ?? entry.resource_id))}`}
                title={reason} disabled={disabled} onClick={() => void removeTerminal(entry, action)}>
                <span className="codicon codicon-trash" aria-hidden="true" />
              </button>
              </div>;
            })}
            {entries.length === 0 ? (
              <div className="terminal-panel-empty-list">
              {t("bottom.terminal.empty")}
              </div>
            ) : null}
          </div>
        </aside>
      </div>
    </>
  );
}
