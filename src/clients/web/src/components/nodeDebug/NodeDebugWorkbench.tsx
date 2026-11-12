import { t } from "../../../../shared/core/i18n/index.js";
import { lazy, Suspense } from "react";
import type { NodeDebugController } from "../../hooks/nodeDebug/useNodeDebugController";
import type { Session } from "../../types/backend";

const NodeDebugPanel = lazy(() => import("./NodeDebugPanel"));

interface NodeDebugWorkbenchProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string | null;
  mainThreadId: string | null;
  activeFilePath: string | null;
  nodeDebugController: NodeDebugController;
  sessions: Session[];
  compact?: boolean;
  onOpenExtensionWindow?: () => void;
  onSelectThread: (threadId: string) => void;
  onOpenWorkspacePath: (path: string) => Promise<void>;
  onStatusChange: (message: string) => void;
}

export default function NodeDebugWorkbench({
  apiPort,
  workspaceId,
  sessionId,
  threadId,
  mainThreadId,
  activeFilePath,
  nodeDebugController,
  sessions,
  compact = false,
  onOpenExtensionWindow,
  onSelectThread,
  onOpenWorkspacePath,
  onStatusChange,
}: NodeDebugWorkbenchProps) {
  return (
    <aside className="debug-panel" aria-label={t("debug.workbench.aria")}>
      <header className="debug-workbench-header">
        <div>
          <strong>{compact ? t("debug.workbench.compact") : t("debug.workbench.title")}</strong>
          <span>{sessionId ? (threadId ? t("debug.workbench.owner", threadId) : t("debug.workbench.resolving")) : t("debug.workbench.noSession")}</span>
        </div>
        {sessionId && threadId && mainThreadId && threadId !== mainThreadId ? (
          <button type="button" onClick={() => onSelectThread(mainThreadId)}>{t("debug.workbench.mainThread")}</button>
        ) : null}
      </header>
      <Suspense fallback={threadId ? (
        <section
          className={`node-debug-panel${compact ? " compact" : " extension-window"}`}
          role="status"
          aria-label={t("debug.panel.aria")}
        >{t("debug.panel.loading")}</section>
      ) : null}>
        {threadId ? <NodeDebugPanel
          apiPort={apiPort}
          workspaceId={workspaceId}
          sessionId={sessionId}
          threadId={threadId}
          activeFilePath={activeFilePath}
          controller={nodeDebugController}
          sessions={sessions}
          extensionWindow={!compact}
          onOpenExtensionWindow={onOpenExtensionWindow}
          onOpenWorkspacePath={onOpenWorkspacePath}
          onStatusChange={onStatusChange}
        /> : null}
      </Suspense>
    </aside>
  );
}
