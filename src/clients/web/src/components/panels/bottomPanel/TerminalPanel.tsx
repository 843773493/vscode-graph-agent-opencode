import { useEffect, useMemo, useRef, useState } from "react";
import type { WorkspaceTerminalController } from "../../../hooks/workspace/useWorkspaceTerminalResources";
import type { ResourceAction, ResourceThreadRef } from "../../../api/workspaceResources";
import { buildGatewayAttachUrl } from "../../../utils/attachUrls";
import { errorMessage } from "../../../utils/errorMessage";
import { statusLabel, stripTerminalNamePrefix } from "../../../state/display/resourceDisplay";

/** 终端 iframe 等待就绪的上限；超过即视为无响应，必须给用户明确的兜底说明与重试入口。 */
export const TERMINAL_FRAME_READY_TIMEOUT_MS = 15_000;

type TerminalFrameStatus = "loading" | "ready" | "stalled";

interface TerminalPanelProps {
  controller: WorkspaceTerminalController;
  currentThread: ResourceThreadRef | null;
  threadTargetUnavailable: boolean;
  workspaceId: string | null;
  workspaceName: string;
  selectedTerminalId: string | null;
  height: number;
  onSelectTerminal: (terminalId: string) => void;
  onSwitchToOutput: () => void;
  onSwitchToPorts: () => void;
  onSwitchToAutomation: () => void;
  onClose: () => void;
}

export default function TerminalPanel({
  controller,
  currentThread,
  threadTargetUnavailable,
  workspaceId,
  workspaceName,
  selectedTerminalId,
  height,
  onSelectTerminal,
  onSwitchToOutput,
  onSwitchToPorts,
  onSwitchToAutomation,
  onClose,
}: TerminalPanelProps) {
  const { items: entries, loading } = controller;
  const onRefresh = () => void controller.refresh();
  const [scope, setScope] = useState<"thread" | "workspace">("thread");
  const acting = controller.mutating;
  const [terminalListWidth, setTerminalListWidth] = useState<number | null>(null);
  const [frameStatus, setFrameStatus] = useState<TerminalFrameStatus>("loading");
  const [frameReloadToken, setFrameReloadToken] = useState(0);
  const resizingRef = useRef(false);
  const selectedEntry = useMemo(
    () => entries.find(
      (entry) => entry.resource_id === selectedTerminalId,
    ) ?? entries[0] ?? null,
    [entries, selectedTerminalId],
  );
  const isCurrentAssociated = selectedEntry?.thread_associations.some((ref) => ref.session_id === currentThread?.session_id && ref.thread_id === currentThread?.thread_id);
  const canAttachCurrent = currentThread && !isCurrentAssociated && (selectedEntry?.retention_scope === "workspace"
    || (selectedEntry?.source_thread?.session_id === currentThread.session_id && selectedEntry?.source_thread?.thread_id === currentThread.thread_id));
  const canControl = (action: ResourceAction) => !acting && !threadTargetUnavailable && selectedEntry?.available_actions.includes(action);
  // 终端服务未就绪时 iframe 只会长时间空白，不会触发 onError；因此必须自行计时，
  // 把「一直转圈」变成明确的「超时 + 重试」。URL 构造失败同样不得在 render 阶段抛出。
  const attach = useMemo((): { url: string } | { error: string } | null => {
    if (!selectedEntry) return null;
    if (threadTargetUnavailable) return { error: "当前子线程目标尚未解析或属于其他工作区，终端操作已停用。" };
    if (selectedEntry.reconciliation_status !== "verified") return { error: "终端运行状态尚待核实，请先查看资源错误并刷新。" };
    if (selectedEntry.status !== "running") return { error: "终端进程已停止，资源记录与最终输出仍然保留。" };
    if (selectedEntry.retention_scope === "thread" && selectedEntry.thread_associations.length === 0) {
      return { error: "临时终端当前没有线程关联；请关联来源线程或保留到工作区后再连接。" };
    }
    if (selectedEntry.retention_scope === "thread" && !currentThread) {
      return { error: "当前没有已解析的会话线程，暂时无法连接临时终端。" };
    }
    if (selectedEntry.retention_scope === "thread" && !selectedEntry.thread_associations.some(
      (ref) => ref.session_id === currentThread?.session_id && ref.thread_id === currentThread.thread_id,
    )) {
      return { error: "临时终端尚未关联当前会话线程，请先关联或切回已关联的线程。" };
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

  const control = async (action: ResourceAction, thread?: ResourceThreadRef) => {
    if (!selectedEntry || threadTargetUnavailable) return;
    try { await controller.control(selectedEntry, action, thread); }
    catch { /* controller 已显示 owner 原始错误并重取完整资源。 */ }
  };
  const create = async () => {
    if (!currentThread || threadTargetUnavailable) return;
    try { onSelectTerminal((await controller.create(currentThread, scope)).resource_id); }
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
    <section
      className="terminal-panel"
      style={{ flexBasis: `${height}px` }}
      data-testid="terminal-panel"
    >
      <header className="terminal-panel-header">
        <div className="terminal-panel-tabs" role="tablist" aria-label="底部面板">
          <button type="button" className="terminal-panel-tab active" role="tab" aria-selected="true">
            <span className="codicon codicon-terminal" aria-hidden="true" />
            <span>终端</span>
            <span className="terminal-panel-count">{entries.length}</span>
          </button>
          <button
            type="button"
            className="terminal-panel-tab"
            role="tab"
            aria-selected="false"
            onClick={onSwitchToOutput}
          >
            <span className="codicon codicon-output" aria-hidden="true" />
            <span>输出</span>
          </button>
          <button
            type="button"
            className="terminal-panel-tab"
            role="tab"
            aria-selected="false"
            onClick={onSwitchToPorts}
          >
            <span className="codicon codicon-server-environment" aria-hidden="true" />
            <span>端口</span>
          </button>
          <button
            type="button"
            className="terminal-panel-tab"
            role="tab"
            aria-selected="false"
            onClick={onSwitchToAutomation}
          >
            <span className="codicon codicon-gear" aria-hidden="true" />
            <span>自动化</span>
          </button>
        </div>
        <div className="terminal-panel-actions">
          <span className="terminal-panel-context" title={workspaceId ?? "未选择工作区"}>
            {workspaceName || "未选择工作区"}
          </span>
          <button
            type="button"
            className="terminal-panel-icon-button"
            title="刷新当前工作区终端"
            aria-label="刷新当前工作区终端"
            disabled={loading}
            onClick={onRefresh}
          >
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="terminal-panel-icon-button"
            title="切换底部面板"
            aria-label="切换底部面板"
            onClick={onClose}
          >
            <span className="codicon codicon-close" aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="terminal-panel-resource-actions">
        <select aria-label="终端保留范围" value={scope} onChange={(event) => setScope(event.target.value as "thread" | "workspace")}>
          <option value="thread">临时终端</option><option value="workspace">工作区保留</option>
        </select>
        <button type="button" disabled={acting || !currentThread || threadTargetUnavailable} onClick={() => void create()}>新建终端</button>
        {selectedEntry ? <>
          <span>{selectedEntry.retention_scope === "workspace" ? "工作区保留" : "随来源线程释放"} · {statusLabel(selectedEntry.status)}</span>
          {selectedEntry.retention_scope === "thread" ? <button type="button" disabled={!canControl("promote")} onClick={() => void control("promote")}>保留到工作区</button> : null}
          <button type="button" disabled={!canControl("attach") || !canAttachCurrent} onClick={() => currentThread && void control("attach", currentThread)}>{isCurrentAssociated ? "已关联当前会话" : "关联当前会话"}</button>
          <button type="button" disabled={!canControl("stop") || selectedEntry.status !== "running"} onClick={() => void control("stop")}>停止</button>
          <button type="button" disabled={!canControl("delete") || selectedEntry.thread_associations.length > 0} title={selectedEntry.thread_associations.length > 0 ? "请先解除所有线程关联" : "删除终端与进程"} onClick={() => void control("delete")}>删除</button>
          {selectedEntry.thread_associations.map((ref) => <span className="terminal-panel-association" key={`${ref.session_id}/${ref.thread_id}`} title={`${ref.session_id} / ${ref.thread_id}`}>
            <span>{ref.session_id === currentThread?.session_id ? "当前会话" : `会话 ${ref.session_id.slice(-8)}`} · 线程 {ref.thread_id.slice(-8)}</span>
            <button type="button" disabled={!canControl("detach")} onClick={() => void control("detach", ref)}>解除关联</button>
          </span>)}
          {selectedEntry.reclaim_at ? <span>无关联，将于 {new Date(selectedEntry.reclaim_at).toLocaleTimeString()} 回收</span> : null}
          {selectedEntry.error ? <span role="alert">{selectedEntry.error}</span> : null}
        </> : null}
      </div>
      {controller.error ? <div role="alert" className="terminal-panel-frame-fallback">{controller.error}</div> : null}

      <div className="terminal-panel-content">
        <aside
          className="terminal-panel-list"
          aria-label="当前工作区终端列表"
          style={{ flex: terminalListWidth === null ? "1 1 0" : `0 0 ${terminalListWidth}px` }}
        >
          <div className="terminal-panel-list-scroll">
            {entries.map((entry) => (
              <button
                type="button"
                key={entry.resource_id}
                className={entry.resource_id === selectedEntry?.resource_id ? "active" : ""}
                onClick={() => onSelectTerminal(entry.resource_id)}
                title={`${String(entry.metadata?.title ?? entry.resource_id)} · ${entry.retention_scope === "workspace" ? "工作区保留" : "临时终端"}`}
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
                当前工作区还没有终端
              </div>
            ) : null}
          </div>
        </aside>

        <div
          className="terminal-panel-sash"
          role="separator"
          aria-label="调整终端列表宽度"
          aria-orientation="vertical"
          tabIndex={0}
          onPointerDown={startListResize}
          onPointerMove={resizeTerminalList}
          onPointerUp={stopListResize}
          onPointerCancel={stopListResize}
        />

        <article className="terminal-panel-viewer" aria-label="终端内容">
          {selectedEntry ? (
            <div className="terminal-panel-frame-host">
              {attach && "url" in attach ? (
                <iframe
                  key={`${attach.url}#${frameReloadToken}`}
                  src={attach.url}
                  className="terminal-panel-frame"
                  title={`${String(selectedEntry.metadata?.title ?? selectedEntry.resource_id)} · ${selectedEntry.retention_scope === "workspace" ? "工作区保留" : "临时终端"}`}
                  onLoad={() => setFrameStatus("ready")}
                  onError={() => setFrameStatus("stalled")}
                />
              ) : null}
              {frameStatus === "stalled" ? (
                <div className="terminal-panel-frame-fallback" role="alert">
                  <strong>
                    {attach && "error" in attach
                      ? "终端暂不可连接"
                      : "终端连接超过 15 秒仍未就绪"}
                  </strong>
                  <span>
                    {attach && "error" in attach
                      ? attach.error
                      : "终端辅助服务可能仍在启动，或该终端已不可连接。"}
                  </span>
                  {attach && "error" in attach && typeof selectedEntry.metadata?.display_buffer === "string" ? <pre className="terminal-panel-final-output">{selectedEntry.metadata.display_buffer}</pre> : null}
                  {attach && "error" in attach ? null : (
                    <button
                      type="button"
                      onClick={() => setFrameReloadToken((token) => token + 1)}
                    >
                      重试连接
                    </button>
                  )}
                </div>
              ) : null}
            </div>
          ) : (
            <div className="terminal-panel-viewer-empty">
              <strong>当前工作区还没有终端</strong>
            </div>
          )}
        </article>
      </div>
    </section>
  );
}
