import { useEffect, useMemo, useRef, useState } from "react";
import type { GatewayExtensionResourceEntry } from "../../../hooks/gatewayExtensions/useGatewayExtensionResources";
import { buildGatewayAttachUrl } from "../../../utils/attachUrls";
import { errorMessage } from "../../../utils/errorMessage";
import { stripTerminalNamePrefix } from "../../../state/display/resourceDisplay";

/** 终端 iframe 等待就绪的上限；超过即视为无响应，必须给用户明确的兜底说明与重试入口。 */
export const TERMINAL_FRAME_READY_TIMEOUT_MS = 15_000;

type TerminalFrameStatus = "loading" | "ready" | "stalled";

interface TerminalPanelProps {
  entries: GatewayExtensionResourceEntry[];
  workspaceId: string | null;
  workspaceName: string;
  selectedTerminalId: string | null;
  height: number;
  loading: boolean;
  onSelectTerminal: (terminalId: string) => void;
  onRefresh: () => void;
  onSwitchToOutput: () => void;
  onSwitchToPorts: () => void;
  onSwitchToAutomation: () => void;
  onClose: () => void;
}

export default function TerminalPanel({
  entries,
  workspaceId,
  workspaceName,
  selectedTerminalId,
  height,
  loading,
  onSelectTerminal,
  onRefresh,
  onSwitchToOutput,
  onSwitchToPorts,
  onSwitchToAutomation,
  onClose,
}: TerminalPanelProps) {
  const [terminalListWidth, setTerminalListWidth] = useState<number | null>(null);
  const [frameStatus, setFrameStatus] = useState<TerminalFrameStatus>("loading");
  const [frameReloadToken, setFrameReloadToken] = useState(0);
  const resizingRef = useRef(false);
  const selectedEntry = useMemo(
    () => entries.find(
      (entry) => entry.resource.resource_id === selectedTerminalId,
    ) ?? entries[0] ?? null,
    [entries, selectedTerminalId],
  );
  // 终端服务未就绪时 iframe 只会长时间空白，不会触发 onError；因此必须自行计时，
  // 把「一直转圈」变成明确的「超时 + 重试」。URL 构造失败同样不得在 render 阶段抛出。
  const attach = useMemo((): { url: string } | { error: string } | null => {
    if (!selectedEntry) return null;
    try {
      return {
        url: buildGatewayAttachUrl(
          "terminal",
          selectedEntry.workspace_id,
          selectedEntry.resource.resource_id,
          true,
        ),
      };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  }, [selectedEntry]);

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
                key={entry.key}
                className={entry.resource.resource_id === selectedEntry?.resource.resource_id ? "active" : ""}
                onClick={() => onSelectTerminal(entry.resource.resource_id)}
                title={`${entry.resource.name} · ${entry.session_title}`}
              >
                <span className="codicon codicon-terminal" aria-hidden="true" />
                <span className="terminal-panel-list-copy">
                  <span className="terminal-panel-list-name">
                    {stripTerminalNamePrefix(entry.resource.name)}
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
                  title={`${selectedEntry.resource.name} · ${selectedEntry.session_title}`}
                  onLoad={() => setFrameStatus("ready")}
                  onError={() => setFrameStatus("stalled")}
                />
              ) : null}
              {frameStatus === "stalled" ? (
                <div className="terminal-panel-frame-fallback" role="alert">
                  <strong>
                    {attach && "error" in attach
                      ? "无法构造终端连接地址"
                      : "终端连接超过 15 秒仍未就绪"}
                  </strong>
                  <span>
                    {attach && "error" in attach
                      ? attach.error
                      : "终端辅助服务可能仍在启动，或该终端已不可连接。"}
                  </span>
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
              <strong>当前工作区没有运行中的终端</strong>
            </div>
          )}
        </article>
      </div>
    </section>
  );
}
