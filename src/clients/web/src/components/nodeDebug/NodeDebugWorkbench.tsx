import type { NodeDebugController } from "../../hooks/useNodeDebugController";
import type { Session } from "../../types/backend";
import NodeDebugPanel from "./NodeDebugPanel";

interface NodeDebugWorkbenchProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string;
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
    <aside className="debug-panel" aria-label="目标程序调试工作台">
      <header className="debug-workbench-header">
        <div>
          <strong>{compact ? "调试" : "目标程序调试"}</strong>
          <span>{sessionId ? `调试 owner: ${threadId}` : "未选择会话"}</span>
        </div>
        {sessionId && threadId !== "main" ? (
          <button type="button" onClick={() => onSelectThread("main")}>切回主线程</button>
        ) : null}
      </header>
      <NodeDebugPanel
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
      />
    </aside>
  );
}
