import BottomPanelTabs from "./BottomPanelTabs";
import type { WebUiBottomPanelTab } from "../../../types/backend";
import type { GatewayWorkspace } from "../../../types/backend";
import type { SessionGeneratorResourcesController } from "../../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import SessionGeneratorManager from "../../agentSessions/SessionGeneratorManager";
import PersistentTasks from "../../workspace/persistentTasks/PersistentTasks";

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
  const workspace = workspaces.find((item) => item.workspace_id === activeWorkspaceId);
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
        {workspace ? <PersistentTasks key={`${apiPort}:${workspace.workspace_id}:${workspace.backend_workspace_id}:${workspace.connection_kind}`} apiPort={apiPort} target={workspace} /> : <p>请先选择工作区。</p>}
        {/* TODO：定时生成器完成统一任务owner迁移后，将会话动作接入同一表单并移除旧入口。 */}
        <details><summary>定时会话生成</summary>
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
        </details>
      </div>
    </section>
  );
}
