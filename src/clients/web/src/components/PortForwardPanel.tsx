import type { GatewayWorkspace } from "../types/backend";
import WorkspacePortForwardPanel from "./workspace/WorkspacePortForwardPanel";

interface PortForwardPanelProps {
  apiPort: number;
  workspace: GatewayWorkspace | null;
  height: number;
  onSwitchToTerminal: () => void;
  onSwitchToOutput: () => void;
  onSwitchToAutomation: () => void;
  onClose: () => void;
}

export default function PortForwardPanel({
  apiPort,
  workspace,
  height,
  onSwitchToTerminal,
  onSwitchToOutput,
  onSwitchToAutomation,
  onClose,
}: PortForwardPanelProps) {
  return (
    <section
      className="port-forward-bottom-panel"
      style={{ flexBasis: `${height}px` }}
      data-testid="port-forward-panel"
    >
      <header className="port-forward-bottom-panel-header">
        <div className="port-forward-bottom-panel-tabs" role="tablist" aria-label="底部面板">
          <button
            type="button"
            className="port-forward-bottom-panel-tab"
            role="tab"
            aria-selected="false"
            onClick={onSwitchToTerminal}
          >
            <span className="codicon codicon-terminal" aria-hidden="true" />
            <span>终端</span>
          </button>
          <button
            type="button"
            className="port-forward-bottom-panel-tab"
            role="tab"
            aria-selected="false"
            onClick={onSwitchToOutput}
          >
            <span className="codicon codicon-output" aria-hidden="true" />
            <span>输出</span>
          </button>
          <button
            type="button"
            className="port-forward-bottom-panel-tab active"
            role="tab"
            aria-selected="true"
          >
            <span className="codicon codicon-server-environment" aria-hidden="true" />
            <span>端口</span>
          </button>
          <button
            type="button"
            className="port-forward-bottom-panel-tab"
            role="tab"
            aria-selected="false"
            onClick={onSwitchToAutomation}
          >
            <span className="codicon codicon-gear" aria-hidden="true" />
            <span>自动化</span>
          </button>
        </div>
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
