import type { GatewayWorkspace } from "../types/backend";
import type { SessionGeneratorResourcesController } from "../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import SessionGeneratorManager from "./agentSessions/SessionGeneratorManager";

interface AutomationPanelProps {
  apiPort: number;
  generatorResources: SessionGeneratorResourcesController;
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  currentSessionId: string;
  workspaceName: string;
  height: number;
  onStatusChange: (message: string) => void;
  onOpenConnectionManager: () => void;
  onReconnectWorkspace: (workspaceId: string) => Promise<void>;
  onStartWorkspace: (workspaceId: string) => Promise<void>;
  onSwitchToTerminal: () => void;
  onSwitchToOutput: () => void;
  onSwitchToPorts: () => void;
  onClose: () => void;
}

export default function AutomationPanel({
  apiPort,
  generatorResources,
  workspaces,
  activeWorkspaceId,
  currentSessionId,
  workspaceName,
  height,
  onStatusChange,
  onOpenConnectionManager,
  onReconnectWorkspace,
  onStartWorkspace,
  onSwitchToTerminal,
  onSwitchToOutput,
  onSwitchToPorts,
  onClose,
}: AutomationPanelProps) {
  return (
    <section
      className="automation-bottom-panel"
      style={{ flexBasis: `${height}px` }}
      data-testid="automation-panel"
    >
      <header className="automation-bottom-panel-header">
        <div className="automation-bottom-panel-tabs" role="tablist" aria-label="底部面板">
          <button type="button" className="automation-bottom-panel-tab" role="tab" aria-selected="false" onClick={onSwitchToTerminal}>
            <span className="codicon codicon-terminal" aria-hidden="true" />
            <span>终端</span>
          </button>
          <button type="button" className="automation-bottom-panel-tab" role="tab" aria-selected="false" onClick={onSwitchToOutput}>
            <span className="codicon codicon-output" aria-hidden="true" />
            <span>输出</span>
          </button>
          <button type="button" className="automation-bottom-panel-tab" role="tab" aria-selected="false" onClick={onSwitchToPorts}>
            <span className="codicon codicon-server-environment" aria-hidden="true" />
            <span>端口</span>
          </button>
          <button type="button" className="automation-bottom-panel-tab active" role="tab" aria-selected="true">
            <span className="codicon codicon-gear" aria-hidden="true" />
            <span>自动化</span>
          </button>
        </div>
        <div className="automation-bottom-panel-actions">
          <span className="automation-bottom-panel-context" title={activeWorkspaceId ?? "未选择工作区"}>
            {workspaceName || "未选择工作区"}
          </span>
          <button type="button" className="automation-bottom-panel-icon-button" title="关闭底部面板" aria-label="关闭底部面板" onClick={onClose}>
            <span className="codicon codicon-close" aria-hidden="true" />
          </button>
        </div>
      </header>
      <div className="automation-bottom-panel-content">
        <SessionGeneratorManager
          apiPort={apiPort}
          generatorResources={generatorResources}
          workspaces={workspaces}
          activeWorkspaceId={activeWorkspaceId}
          currentSessionId={currentSessionId}
          onStatusChange={onStatusChange}
          onOpenConnectionManager={onOpenConnectionManager}
          onReconnectWorkspace={onReconnectWorkspace}
          onStartWorkspace={onStartWorkspace}
        />
      </div>
    </section>
  );
}
