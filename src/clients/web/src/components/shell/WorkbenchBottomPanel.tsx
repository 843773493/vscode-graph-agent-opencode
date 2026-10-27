import { lazy, Suspense, type PointerEvent as ReactPointerEvent } from "react";
import type { WorkspaceTerminalController } from "../../hooks/workspace/useWorkspaceTerminalResources";
import type { ResourceThreadRef } from "../../api/workspaceResources";
import type { TaskThreadTargetDTO } from "../../api/persistentTasks";
import type { OutputDiagnostic, WorkspaceBottomPanelState } from "../../state/workspaceBottomPanel";
import type { GatewayWorkspace } from "../../types/backend";

const McpConnectionsPanel = lazy(() => import("../panels/bottomPanel/McpConnectionsPanel"));
const GatewayLogPanel = lazy(() => import("../panels/bottomPanel/GatewayLogPanel"));
const AutomationPanel = lazy(() => import("../panels/bottomPanel/AutomationPanel"));
const PortForwardPanel = lazy(() => import("../panels/bottomPanel/PortForwardPanel"));
const TerminalPanel = lazy(() => import("../panels/bottomPanel/TerminalPanel"));

function BottomPanelLoading({ height }: { height: number }) {
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        flexBasis: `${height}px`,
        minHeight: 190,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        borderTop: "1px solid var(--bt-panel-border)",
        background: "var(--bt-bottom-panel-background)",
        color: "var(--bt-text-muted)",
      }}
    >
      正在加载底部面板…
    </div>
  );
}

interface WorkbenchBottomPanelProps {
  visible: boolean;
  state: WorkspaceBottomPanelState;
  apiPort: number;
  workspaceId: string | null;
  diagnostic: OutputDiagnostic | null;
  workspace: GatewayWorkspace | null;
  workspaceName: string;
  terminalController: WorkspaceTerminalController;
  terminalThread: ResourceThreadRef | null;
  terminalThreadTargetUnavailable: boolean;
  onUpdateState: (patch: Partial<WorkspaceBottomPanelState>) => void;
  onStartResize: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onResetHeight: () => void;
  onOpenSession: (workspaceId: string, sessionId: string) => Promise<void>;
  goalSessionId: string | null;
  onManageGoal: (sessionId: string) => Promise<void>;
  onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
  onStatusChange: (text: string) => void;
}

/**
 * 主窗口底部面板：拖拽分隔条加上「终端 / 输出 / 端口 / 自动化 / MCP 连接」互斥标签。
 * 面板状态按工作区归属，由调用方传入；本组件只负责按当前标签渲染对应面板并回写状态。
 */
export default function WorkbenchBottomPanel({
  visible,
  state,
  apiPort,
  workspaceId,
  diagnostic,
  workspace,
  workspaceName,
  terminalController,
  terminalThread,
  terminalThreadTargetUnavailable,
  onUpdateState,
  onStartResize,
  onResetHeight,
  onOpenSession,
  goalSessionId,
  onManageGoal,
  onOpenThread,
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
      <Suspense fallback={<BottomPanelLoading height={state.height} />}>
        {state.tab === "terminal" ? (
          <TerminalPanel
            controller={terminalController}
            currentThread={terminalThread}
            threadTargetUnavailable={terminalThreadTargetUnavailable}
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            selectedTerminalId={state.terminalId}
            height={state.height}
            onSelectTerminal={(terminalId) => onUpdateState({
              tab: "terminal",
              terminalId,
            })}
            onSwitchTab={switchTab}
            onClose={close}
          />
        ) : state.tab === "ports" ? (
          <PortForwardPanel
            apiPort={apiPort}
            workspace={workspace}
            height={state.height}
            onSwitchTab={switchTab}
            onClose={close}
          />
        ) : state.tab === "automation" ? (
          <AutomationPanel
            apiPort={apiPort}
            workspace={workspace}
            workspaceName={workspace?.name ?? workspaceName}
            height={state.height}
            onStatusChange={onStatusChange}
            onOpenSession={onOpenSession}
            goalSessionId={goalSessionId}
            onManageGoal={onManageGoal}
            onOpenThread={onOpenThread}
            onSwitchTab={switchTab}
            onClose={close}
          />
        ) : state.tab === "mcp" ? (
          <McpConnectionsPanel apiPort={apiPort} workspace={workspace} height={state.height}
            onSwitchTab={switchTab} onClose={close} />
        ) : (
          <GatewayLogPanel
            apiPort={apiPort}
            workspaceId={workspaceId}
            diagnostic={diagnostic}
            height={state.height}
            onSwitchTab={switchTab}
            onClose={close}
          />
        )}
      </Suspense>
    </>
  );
}
