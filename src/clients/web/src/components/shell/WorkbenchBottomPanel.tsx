import type { PointerEvent as ReactPointerEvent } from "react";
import GatewayLogPanel from "../panels/bottomPanel/GatewayLogPanel";
import AutomationPanel from "../panels/bottomPanel/AutomationPanel";
import PortForwardPanel from "../panels/bottomPanel/PortForwardPanel";
import TerminalPanel from "../panels/bottomPanel/TerminalPanel";
import type { SessionGeneratorResourcesController } from "../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import type { GatewayExtensionResourceEntry } from "../../hooks/gatewayExtensions/useGatewayExtensionResources";
import type { WorkspaceBottomPanelState } from "../../state/workspaceBottomPanel";
import type { GatewayWorkspace } from "../../types/backend";

interface WorkbenchBottomPanelProps {
  visible: boolean;
  state: WorkspaceBottomPanelState;
  apiPort: number;
  workspaceId: string | null;
  workspace: GatewayWorkspace | null;
  workspaceName: string;
  currentSessionId: string;
  terminalEntries: GatewayExtensionResourceEntry[];
  terminalLoading: boolean;
  generatorResources: SessionGeneratorResourcesController;
  workspaces: GatewayWorkspace[];
  onUpdateState: (patch: Partial<WorkspaceBottomPanelState>) => void;
  onStartResize: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onResetHeight: () => void;
  onRefreshTerminals: () => void;
  onOpenConnectionManager: () => void;
  onReconnectWorkspace: (workspaceId: string) => Promise<void>;
  onStartWorkspace: (workspaceId: string) => Promise<void>;
  onStatusChange: (text: string) => void;
}

/**
 * 主窗口底部面板：拖拽分隔条加上「终端 / 端口 / 自动化 / 工作区输出」四个互斥标签。
 * 面板状态按工作区归属，由调用方传入；本组件只负责按当前标签渲染对应面板并回写状态。
 */
export default function WorkbenchBottomPanel({
  visible,
  state,
  apiPort,
  workspaceId,
  workspace,
  workspaceName,
  currentSessionId,
  terminalEntries,
  terminalLoading,
  generatorResources,
  workspaces,
  onUpdateState,
  onStartResize,
  onResetHeight,
  onRefreshTerminals,
  onOpenConnectionManager,
  onReconnectWorkspace,
  onStartWorkspace,
  onStatusChange,
}: WorkbenchBottomPanelProps) {
  if (!visible) {
    return null;
  }

  const switchTab = (tab: WorkspaceBottomPanelState["tab"]) => {
    onUpdateState({ tab });
  };
  const close = () => onUpdateState({ visible: false });

  return (
    <>
      <button
        type="button"
        className="layout-sash layout-sash-gateway-panel"
        title="拖拽调整底部面板高度，双击还原"
        aria-label="调整底部面板高度"
        onPointerDown={onStartResize}
        onDoubleClick={onResetHeight}
      />
      {state.tab === "terminal" ? (
        <TerminalPanel
          entries={terminalEntries}
          workspaceId={workspaceId}
          workspaceName={workspaceName}
          selectedTerminalId={state.terminalId}
          height={state.height}
          loading={terminalLoading}
          onSelectTerminal={(terminalId) => onUpdateState({
            tab: "terminal",
            terminalId,
          })}
          onRefresh={onRefreshTerminals}
          onSwitchToOutput={() => switchTab("output")}
          onSwitchToPorts={() => switchTab("ports")}
          onSwitchToAutomation={() => switchTab("automation")}
          onClose={close}
        />
      ) : state.tab === "ports" ? (
        <PortForwardPanel
          apiPort={apiPort}
          workspace={workspace}
          height={state.height}
          onSwitchToTerminal={() => switchTab("terminal")}
          onSwitchToOutput={() => switchTab("output")}
          onSwitchToAutomation={() => switchTab("automation")}
          onClose={close}
        />
      ) : state.tab === "automation" ? (
        <AutomationPanel
          apiPort={apiPort}
          generatorResources={generatorResources}
          workspaces={workspaces}
          activeWorkspaceId={workspaceId}
          currentSessionId={currentSessionId}
          workspaceName={workspace?.name ?? workspaceName}
          height={state.height}
          onStatusChange={onStatusChange}
          onOpenConnectionManager={onOpenConnectionManager}
          onReconnectWorkspace={onReconnectWorkspace}
          onStartWorkspace={onStartWorkspace}
          onSwitchToTerminal={() => switchTab("terminal")}
          onSwitchToOutput={() => switchTab("output")}
          onSwitchToPorts={() => switchTab("ports")}
          onClose={close}
        />
      ) : (
        <GatewayLogPanel
          apiPort={apiPort}
          workspaceId={workspaceId}
          height={state.height}
          onOpenTerminal={() => switchTab("terminal")}
          onOpenPorts={() => switchTab("ports")}
          onOpenAutomation={() => switchTab("automation")}
          onClose={close}
        />
      )}
    </>
  );
}
