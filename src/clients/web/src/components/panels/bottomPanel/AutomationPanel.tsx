import BottomPanelTabs from "./BottomPanelTabs";
import type { WebUiBottomPanelTab } from "../../../types/backend";
import type { GatewayWorkspace } from "../../../types/backend";
import type { SessionGeneratorResourcesController } from "../../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import SessionGeneratorManager from "../../agentSessions/SessionGeneratorManager";

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
  onSwitchTab: (tab: WebUiBottomPanelTab) => void;
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
  onSwitchTab,
  onClose,
}: AutomationPanelProps) {
  return (
    <section
      className="automation-bottom-panel"
      style={{ flexBasis: `${height}px` }}
      data-testid="automation-panel"
    >
      <header className="automation-bottom-panel-header">
        <BottomPanelTabs selected="automation" prefix="automation-bottom-panel" onSelect={onSwitchTab} />
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
