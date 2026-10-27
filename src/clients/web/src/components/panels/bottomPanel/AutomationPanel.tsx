import BottomPanelTabs from "./BottomPanelTabs";
import type { GatewayWorkspace, WebUiBottomPanelTab } from "../../../types/backend";
import PersistentTasks from "../../workspace/persistentTasks/PersistentTasks";

interface AutomationPanelProps {
  apiPort: number;
  workspace: GatewayWorkspace | null;
  workspaceName: string;
  height: number;
  onStatusChange: (message: string) => void;
  onOpenSession: (workspaceId: string, sessionId: string) => Promise<void>;
  onSwitchTab: (tab: WebUiBottomPanelTab) => void;
  onClose: () => void;
}

export default function AutomationPanel({ apiPort, workspace, workspaceName, height, onStatusChange, onOpenSession, onSwitchTab, onClose }: AutomationPanelProps) {
  return <section className="automation-bottom-panel" style={{ flexBasis: `${height}px` }} data-testid="automation-panel">
    <header className="automation-bottom-panel-header">
      <BottomPanelTabs selected="automation" prefix="automation-bottom-panel" onSelect={onSwitchTab} />
      <div className="automation-bottom-panel-actions">
        <span className="automation-bottom-panel-context" title={workspace?.workspace_id ?? "未选择工作区"}>{workspaceName || "未选择工作区"}</span>
        <button type="button" className="automation-bottom-panel-icon-button" title="关闭底部面板" aria-label="关闭底部面板" onClick={onClose}><span className="codicon codicon-close" aria-hidden="true" /></button>
      </div>
    </header>
    <div className="automation-bottom-panel-content">
      {workspace ? <PersistentTasks key={`${apiPort}:${workspace.workspace_id}:${workspace.backend_workspace_id}:${workspace.connection_kind}`} apiPort={apiPort} target={workspace} onStatusChange={onStatusChange} onOpenSession={(sessionId) => onOpenSession(workspace.workspace_id, sessionId)} /> : <p>请先选择工作区。</p>}
    </div>
  </section>;
}
