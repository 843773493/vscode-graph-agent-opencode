import type { ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import type { WorkspaceResourcesController } from "../../../hooks/workspace/useWorkspaceResources";
import type { ResourceAction, ResourceThreadRef } from "../../../api/workspaceResources";
import { buildGatewayAttachUrl } from "../../../utils/attachUrls";
import { errorMessage } from "../../../utils/errorMessage";
import { formatLocalTime } from "../../../utils/format";
import { statusLabel, stripTerminalNamePrefix } from "../../../state/display/resourceDisplay";

/** 终端 iframe 等待就绪的上限；超过即视为无响应，必须给用户明确的兜底说明与重试入口。 */
export const TERMINAL_FRAME_READY_TIMEOUT_MS = 15_000;

type TerminalFrameStatus = "loading" | "ready" | "stalled";

interface TerminalContentProps {
  controller: WorkspaceResourcesController;
  currentThread: ResourceThreadRef | null;
  threadTargetUnavailable: boolean;
  workspaceId: string | null;
  workspaceName: string;
  selectedTerminalId: string | null;
  onSelectTerminal: (terminalId: string) => void;
  renderToolbar: (tools: ReactNode) => ReactNode;
}

/** 终端产品内容；标签、停靠和关闭由所在工作台提供。 */
export function TerminalContent({
  controller, currentThread, threadTargetUnavailable, workspaceId, workspaceName,
  selectedTerminalId, onSelectTerminal, renderToolbar,
}: TerminalContentProps) {
  const { items: entries, loading } = controller;
  const onRefresh = () => void controller.refresh();
  const [retentionScope, setRetentionScope] = useState<"thread" | "workspace">("thread");
  const isMutating = controller.mutating;
  const [terminalListWidth, setTerminalListWidth] = useState<number | null>(null);
  const [frameStatus, setFrameStatus] = useState<TerminalFrameStatus>("loading");
  const [frameReloadToken, setFrameReloadToken] = useState(0);
  const resizingRef = useRef(false);
  const selectedEntry = useMemo(
    () => selectedTerminalId === null ? entries[0] ?? null : entries.find(
      (entry) => entry.resource_id === selectedTerminalId,
    ) ?? null,
    [entries, selectedTerminalId],
  );
  const terminalErrorMessage = [selectedEntry?.error, controller.error]
    .filter((message): message is string => typeof message === "string" && message.length > 0)
    .join(" · ");
  const isCurrentThreadAssociated = selectedEntry?.thread_associations.some((ref) => ref.session_id === currentThread?.session_id && ref.thread_id === currentThread?.thread_id);
  const canAttachToCurrentThread = currentThread && !isCurrentThreadAssociated && (selectedEntry?.retention_scope === "workspace"
    || (selectedEntry?.source_thread?.session_id === currentThread.session_id && selectedEntry?.source_thread?.thread_id === currentThread.thread_id));
  const canControlSelectedTerminal = (action: ResourceAction) => !isMutating && !threadTargetUnavailable && selectedEntry?.available_actions.includes(action);
  // 终端服务未就绪时 iframe 只会长时间空白，不会触发 onError；因此必须自行计时，
  // 把「一直转圈」变成明确的「超时 + 重试」。URL 构造失败同样不得在 render 阶段抛出。
  const attach = useMemo((): { url: string } | { error: string } | null => {
    if (!selectedEntry) return null;
    if (threadTargetUnavailable) return { error: t("bottom.terminal.attach.threadTargetUnavailable") };
    if (selectedEntry.reconciliation_status !== "verified") return { error: t("bottom.terminal.attach.unverified") };
    if (selectedEntry.status !== "running") return { error: t("bottom.terminal.attach.stopped") };
    if (selectedEntry.retention_scope === "thread" && selectedEntry.thread_associations.length === 0) {
      return { error: t("bottom.terminal.attach.noAssociation") };
    }
    if (selectedEntry.retention_scope === "thread" && !currentThread) {
      return { error: t("bottom.terminal.attach.noCurrentThread") };
    }
    if (selectedEntry.retention_scope === "thread" && !selectedEntry.thread_associations.some(
      (ref) => ref.session_id === currentThread?.session_id && ref.thread_id === currentThread.thread_id,
    )) {
      return { error: t("bottom.terminal.attach.notAssociated") };
    }
    try {
      return {
        url: buildGatewayAttachUrl(
          "terminal",
          workspaceId ?? "",
          selectedEntry.resource_id,
          true,
          selectedEntry.retention_scope === "thread" ? currentThread ?? undefined : undefined,
        ),
      };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  }, [currentThread?.session_id, currentThread?.thread_id, selectedEntry?.resource_id, selectedEntry?.owner_revision, selectedEntry?.status, selectedEntry?.reconciliation_status, selectedEntry?.retention_scope, selectedEntry?.thread_associations, threadTargetUnavailable, workspaceId]);

  useEffect(() => {
    if (!attach || "error" in attach) {
      setFrameStatus("stalled");
      return;
    }
    setFrameStatus("loading");
    const timerId = window.setTimeout(() => {
      setFrameStatus((current) => (current === "loading" ? "stalled" : current));
    }, TERMINAL_FRAME_READY_TIMEOUT_MS);
    return () => window.clearTimeout(timerId);
  }, [attach, frameReloadToken]);

  const controlSelectedTerminal = async (action: ResourceAction, thread?: ResourceThreadRef) => {
    if (!selectedEntry || threadTargetUnavailable) return;
    try { await controller.control(selectedEntry, action, thread); }
    catch { /* controller 已显示 owner 原始错误并重取完整资源。 */ }
  };
  const createTerminal = async () => {
    if (!currentThread || threadTargetUnavailable) return;
    try { onSelectTerminal((await controller.create(currentThread, retentionScope)).resource_id); }
    catch { /* controller 已显示创建错误并重新读取工作区清单。 */ }
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
    const contentLeft = event.currentTarget.parentElement?.getBoundingClientRect().left ?? 0;
    const nextWidth = Math.min(460, Math.max(180, event.clientX - contentLeft));
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
      {renderToolbar(<>
          <span className="workbench-bottom-panel-count" aria-label={t("bottom.terminal.count", entries.length)}>{entries.length}</span>
          <span className="workbench-bottom-panel-context" title={workspaceId ?? t("bottom.common.noWorkspace")}>
            {workspaceName || t("bottom.common.noWorkspace")}
          </span>
          <button
            type="button"
            className="workbench-bottom-panel-icon-button"
            title={t("bottom.terminal.refresh")}
            aria-label={t("bottom.terminal.refresh")}
            disabled={loading}
            onClick={onRefresh}
          >
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
          </button>
      </>)}

      <div className="terminal-panel-resource-actions">
        <select aria-label={t("bottom.terminal.retentionScope")} value={retentionScope} onChange={(event) => setRetentionScope(event.target.value as "thread" | "workspace")}>
          <option value="thread">{t("bottom.terminal.temporary")}</option><option value="workspace">{t("resources.keep")}</option>
        </select>
        <button type="button" disabled={isMutating || !currentThread || threadTargetUnavailable} onClick={() => void createTerminal()}>{t("bottom.terminal.create")}</button>
        {selectedEntry ? <>
          <span>{selectedEntry.retention_scope === "workspace" ? t("resources.keep") : t("resources.releaseWithThread")} · {statusLabel(selectedEntry.status)}</span>
          {selectedEntry.retention_scope === "thread" ? <button type="button" disabled={!canControlSelectedTerminal("promote")} onClick={() => void controlSelectedTerminal("promote")}>{t("bottom.terminal.keep")}</button> : null}
          <button type="button" disabled={!canControlSelectedTerminal("attach") || !canAttachToCurrentThread} onClick={() => currentThread && void controlSelectedTerminal("attach", currentThread)}>{isCurrentThreadAssociated ? t("bottom.terminal.alreadyAssociated") : t("bottom.terminal.associate")}</button>
          <button type="button" disabled={!canControlSelectedTerminal("stop") || selectedEntry.status !== "running"} onClick={() => void controlSelectedTerminal("stop")}>{t("bottom.terminal.stop")}</button>
          <button type="button" disabled={!canControlSelectedTerminal("delete") || selectedEntry.thread_associations.length > 0} title={selectedEntry.thread_associations.length > 0 ? t("bottom.terminal.detachBeforeDelete") : t("bottom.terminal.deleteTitle")} onClick={() => void controlSelectedTerminal("delete")}>{t("bottom.terminal.delete")}</button>
          {selectedEntry.thread_associations.map((ref) => <span className="terminal-panel-association" key={`${ref.session_id}/${ref.thread_id}`} title={`${ref.session_id} / ${ref.thread_id}`}>
            <span>{ref.session_id === currentThread?.session_id ? t("bottom.common.currentSession") : t("bottom.common.session", ref.session_id.slice(-8))} · {t("bottom.common.thread", ref.thread_id.slice(-8))}</span>
            <button type="button" disabled={!canControlSelectedTerminal("detach")} onClick={() => void controlSelectedTerminal("detach", ref)}>{t("bottom.terminal.detach")}</button>
          </span>)}
          {selectedEntry.reclaim_at ? <span>{t("bottom.terminal.reclaimAt", formatLocalTime(selectedEntry.reclaim_at))}</span> : null}
        </> : null}
      </div>
      <div className="terminal-panel-error-slot">
        {terminalErrorMessage ? (
          <div role="alert" title={terminalErrorMessage}>
            {terminalErrorMessage}
          </div>
        ) : null}
      </div>

      <div className="terminal-panel-content">
        <aside
          className="terminal-panel-list"
          aria-label={t("bottom.terminal.list")}
          style={{ flex: terminalListWidth === null ? "1 1 0" : `0 0 ${terminalListWidth}px` }}
        >
          <div className="terminal-panel-list-scroll">
            {entries.map((entry) => (
              <button
                type="button"
                key={entry.resource_id}
                className={entry.resource_id === selectedEntry?.resource_id ? "active" : ""}
                onClick={() => onSelectTerminal(entry.resource_id)}
                title={`${String(entry.metadata?.title ?? entry.resource_id)} · ${entry.retention_scope === "workspace" ? t("resources.keep") : t("bottom.terminal.temporary")}`}
              >
                <span className="codicon codicon-terminal" aria-hidden="true" />
                <span className="terminal-panel-list-copy">
                  <span className="terminal-panel-list-name">
                    {stripTerminalNamePrefix(String(entry.metadata?.title ?? entry.resource_id))}
                  </span>
                </span>
              </button>
            ))}
            {entries.length === 0 ? (
              <div className="terminal-panel-empty-list">
              {t("bottom.terminal.empty")}
              </div>
            ) : null}
          </div>
        </aside>

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
        />

        <article className="terminal-panel-viewer" aria-label={t("bottom.terminal.content")}>
          {selectedEntry ? (
            <div className="terminal-panel-frame-host">
              {attach && "url" in attach ? (
                <iframe
                  key={`${attach.url}#${frameReloadToken}`}
                  src={attach.url}
                  className="terminal-panel-frame"
                  title={`${String(selectedEntry.metadata?.title ?? selectedEntry.resource_id)} · ${selectedEntry.retention_scope === "workspace" ? t("resources.keep") : t("bottom.terminal.temporary")}`}
                  onLoad={() => setFrameStatus("ready")}
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
                  {attach && "error" in attach && typeof selectedEntry.metadata?.display_buffer === "string" ? <pre className="terminal-panel-final-output">{selectedEntry.metadata.display_buffer}</pre> : null}
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
      </div>
    </>
  );
}
