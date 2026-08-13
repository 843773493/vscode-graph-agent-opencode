import type { NodeDebugController } from "../../hooks/useNodeDebugController";
import type { Session } from "../../types/backend";
import NodeDebugPanel from "./NodeDebugPanel";

interface DebugPanelProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  activeFilePath: string | null;
  nodeDebugController: NodeDebugController;
  sessions: Session[];
  compact?: boolean;
  onOpenExtensionWindow?: () => void;
  onOpenWorkspacePath: (path: string) => Promise<void>;
  onStatusChange: (message: string) => void;
}

export default function DebugPanel({
  apiPort,
  workspaceId,
  sessionId,
  activeFilePath,
  nodeDebugController,
  sessions,
  compact = false,
  onOpenExtensionWindow,
  onOpenWorkspacePath,
  onStatusChange,
}: DebugPanelProps) {
  return (
    <aside className="debug-panel" aria-label="目标程序调试工作台">
      <header className="debug-workbench-header">
        <div>
          <strong>{compact ? "调试" : "目标程序调试"}</strong>
          <span>{sessionId ? "AI 与用户共享当前会话方案" : "未选择会话"}</span>
        </div>
      </header>
      <NodeDebugPanel
        apiPort={apiPort}
        workspaceId={workspaceId}
        sessionId={sessionId}
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
