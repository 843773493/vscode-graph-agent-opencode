import BottomPanelTabs from "./BottomPanelTabs";
import type { WebUiBottomPanelTab } from "../../../types/backend";
import type { GatewayWorkspace } from "../../../types/backend";
import WorkspacePortForwardPanel from "../../workspace/portForward/WorkspacePortForwardPanel";

interface PortForwardPanelProps {
  apiPort: number;
  workspace: GatewayWorkspace | null;
  height: number;
  onSwitchTab: (tab: WebUiBottomPanelTab) => void;
  onClose: () => void;
}

export default function PortForwardPanel({
  apiPort,
  workspace,
  height,
  onSwitchTab,
  onClose,
}: PortForwardPanelProps) {
  return (
    <section
      className="port-forward-bottom-panel"
      style={{ flexBasis: `${height}px` }}
      data-testid="port-forward-panel"
    >
      <header className="port-forward-bottom-panel-header">
        <BottomPanelTabs selected="ports" prefix="port-forward-bottom-panel" onSelect={onSwitchTab} />
        <div className="port-forward-bottom-panel-actions">
          <button
            type="button"
            className="port-forward-bottom-panel-icon-button"
            title="关闭底部面板"
            aria-label="关闭底部面板"
            onClick={onClose}
          >
            <span className="codicon codicon-close" aria-hidden="true" />
          </button>
        </div>
      </header>
      <div className="port-forward-bottom-panel-content">
        <WorkspacePortForwardPanel
          apiPort={apiPort}
          workspace={workspace}
          active
        />
      </div>
    </section>
  );
}
