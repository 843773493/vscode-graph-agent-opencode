import type { GatewayWorkspace, WebUiBottomPanelTab } from "../../../types/backend";
import McpConnections from "../../workspace/mcp/McpConnections";
import BottomPanelTabs from "./BottomPanelTabs";

export default function McpConnectionsPanel({ apiPort, workspace, height, onSwitchTab, onClose }: {
  apiPort: number; workspace: GatewayWorkspace | null; height: number;
  onSwitchTab: (tab: WebUiBottomPanelTab) => void; onClose: () => void;
}) {
  return <section className="port-forward-bottom-panel" style={{ flexBasis: `${height}px` }} data-testid="mcp-connections-panel">
    <header className="port-forward-bottom-panel-header">
      <BottomPanelTabs selected="mcp" prefix="port-forward-bottom-panel" onSelect={onSwitchTab} />
      <button type="button" className="port-forward-bottom-panel-icon-button" onClick={onClose} aria-label="关闭底部面板">
        <span className="codicon codicon-close" aria-hidden="true" />
      </button>
    </header>
    <div className="port-forward-bottom-panel-content">
      {workspace ? <McpConnections key={workspace.workspace_id} apiPort={apiPort} target={workspace} /> : <p>请先选择工作区。</p>}
    </div>
  </section>;
}
